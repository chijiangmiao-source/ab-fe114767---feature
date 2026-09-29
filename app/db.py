"""SQLite persistence: link windows, secondary stations and stable receipts.

Every state change -- a frame arriving at either station, a same-origin
snapshot fork, or a post-reconnect convergence -- is resolved inside one
``BEGIN IMMEDIATE`` transaction against the sliding-window state and the
receipt ledger.  A single instance lock plus SQLite write locks serialise
concurrent arrivals and convergences, so the two stations' windows and the
receipt records can never disagree.

A secondary station may only be forked from an explicit snapshot of one of
the link's own windows (a *same-origin* snapshot).  The snapshot's highest
extended sequence and bitmap are copied verbatim: both stations then share
that common base, so extended sequence numbers stay calibrated across the
fork and the two bitmaps can be projected onto one another at convergence.
"""

import hashlib
import json
import sqlite3
import threading
import uuid

from . import window as win

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS link_windows (
    link_id  TEXT PRIMARY KEY REFERENCES links(id),
    highest  INTEGER,
    bitmap   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id   TEXT PRIMARY KEY,
    link_id      TEXT NOT NULL REFERENCES links(id),
    station_id   TEXT,
    counter      INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL,
    extended     INTEGER NOT NULL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id         TEXT PRIMARY KEY,
    link_id    TEXT NOT NULL REFERENCES links(id),
    highest    INTEGER,
    bitmap     INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stations (
    id           TEXT PRIMARY KEY,
    link_id      TEXT NOT NULL REFERENCES links(id),
    name         TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL REFERENCES snapshots(id),
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS station_windows (
    station_id TEXT PRIMARY KEY REFERENCES stations(id),
    highest    INTEGER,
    bitmap     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS convergences (
    id              TEXT PRIMARY KEY,
    link_id         TEXT NOT NULL REFERENCES links(id),
    station_id      TEXT NOT NULL REFERENCES stations(id),
    merged_highest  INTEGER NOT NULL,
    merged_bitmap   INTEGER NOT NULL,
    added_primary   TEXT NOT NULL,
    added_secondary TEXT NOT NULL,
    created_at      REAL NOT NULL
);
"""


class ReceiptConflict(Exception):
    """A stable receipt id was reused with a different link/counter/payload."""


class OriginError(Exception):
    """A snapshot/station from a different link was offered for this link.

    A secondary station must be forked from a same-origin snapshot and later
    converged against that same link; a foreign snapshot cannot be calibrated
    and must not alter any window.
    """


def canonical_hash(payload):
    """Deterministic hash of an arbitrary JSON payload."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Database:
    def __init__(self, path):
        self.path = path
        # check_same_thread=False: http.server spawns a thread per request;
        # the lock + BEGIN IMMEDIATE serialise write transactions.
        # isolation_level=None => autocommit unless we open a transaction
        # explicitly, so all BEGIN/COMMIT boundaries below are real.
        self.conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._lock = threading.Lock()

    def _migrate(self):
        """Add columns introduced after the first release (existing volumes)."""
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(receipts)")}
        if "station_id" not in cols:
            self.conn.execute("ALTER TABLE receipts ADD COLUMN station_id TEXT")

    def close(self):
        with self._lock:
            self.conn.close()

    # ---------------------------------------------------------------- links

    def create_link(self, name):
        link_id = uuid.uuid4().hex
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO links (id, name, created_at) "
                    "VALUES (?, ?, strftime('%s','now'))",
                    (link_id, name),
                )
                self.conn.execute(
                    "INSERT INTO link_windows (link_id, highest, bitmap) "
                    "VALUES (?, NULL, 0)",
                    (link_id,),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.get_link(link_id)

    def get_link(self, link_id):
        row = self.conn.execute(
            "SELECT l.id, l.name, w.highest, w.bitmap FROM links l "
            "JOIN link_windows w ON w.link_id = l.id WHERE l.id = ?",
            (link_id,),
        ).fetchone()
        if row is None:
            return None
        return self._link_state(row)

    def list_links(self):
        rows = self.conn.execute(
            "SELECT l.id, l.name, w.highest, w.bitmap FROM links l "
            "JOIN link_windows w ON w.link_id = l.id ORDER BY l.created_at"
        ).fetchall()
        return [self._link_state(r) for r in rows]

    @staticmethod
    def _link_state(row):
        highest = row["highest"]
        bitmap = row["bitmap"] or 0
        return {
            "id": row["id"],
            "name": row["name"],
            "highest": highest,
            "bitmap": bitmap,
            "recent": win.recent_positions(highest, bitmap),
        }

    # ------------------------------------------------------------ snapshots

    def create_snapshot(self, link_id):
        """Freeze a link's current window into an explicit, immutable snapshot.

        A secondary station can only be forked from one of the link's own
        snapshots, which makes the fork *same-origin* and gives both stations
        a common base for calibrating extended sequence numbers.
        """
        snapshot_id = uuid.uuid4().hex
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT highest, bitmap FROM link_windows WHERE link_id = ?",
                    (link_id,),
                ).fetchone()
                if row is None:
                    raise LookupError(link_id)
                self.conn.execute(
                    "INSERT INTO snapshots (id, link_id, highest, bitmap, "
                    "created_at) VALUES (?, ?, ?, ?, strftime('%s','now'))",
                    (snapshot_id, link_id, row["highest"], row["bitmap"] or 0),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.get_snapshot(snapshot_id)

    def get_snapshot(self, snapshot_id):
        row = self.conn.execute(
            "SELECT id, link_id, highest, bitmap FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        highest, bitmap = row["highest"], row["bitmap"] or 0
        return {
            "id": row["id"],
            "link_id": row["link_id"],
            "highest": highest,
            "bitmap": bitmap,
            "recent": win.recent_positions(highest, bitmap),
        }

    # ------------------------------------------------------------- stations

    def create_station(self, link_id, name, snapshot_id):
        """Fork a secondary station from a same-origin snapshot.

        Raises ``LookupError`` if the link or snapshot does not exist, and
        ``OriginError`` if the snapshot belongs to a different link: a station
        from a foreign origin can never be calibrated against this link and is
        refused before any window is touched.
        """
        station_id = uuid.uuid4().hex
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                snap = self.conn.execute(
                    "SELECT link_id, highest, bitmap FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                ).fetchone()
                if snap is None:
                    raise LookupError(snapshot_id)
                if snap["link_id"] != link_id or not self._link_exists(link_id):
                    raise OriginError(snapshot_id)
                self.conn.execute(
                    "INSERT INTO stations (id, link_id, name, snapshot_id, "
                    "created_at) VALUES (?, ?, ?, ?, strftime('%s','now'))",
                    (station_id, link_id, name[:128], snapshot_id),
                )
                # The snapshot is the common base: the secondary inherits the
                # primary's 64-bit highest/bitmap verbatim, so its extended
                # sequence numbers share one coordinate system.
                self.conn.execute(
                    "INSERT INTO station_windows (station_id, highest, bitmap) "
                    "VALUES (?, ?, ?)",
                    (station_id, snap["highest"], snap["bitmap"] or 0),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.get_station(station_id)

    def _link_exists(self, link_id):
        return self.conn.execute(
            "SELECT 1 FROM links WHERE id = ?", (link_id,)
        ).fetchone() is not None

    def get_station(self, station_id):
        row = self.conn.execute(
            "SELECT s.id, s.link_id, s.name, s.snapshot_id, w.highest, w.bitmap "
            "FROM stations s JOIN station_windows w ON w.station_id = s.id "
            "WHERE s.id = ?",
            (station_id,),
        ).fetchone()
        return self._station_state(row) if row is not None else None

    def list_stations(self, link_id):
        rows = self.conn.execute(
            "SELECT s.id, s.link_id, s.name, s.snapshot_id, w.highest, w.bitmap "
            "FROM stations s JOIN station_windows w ON w.station_id = s.id "
            "WHERE s.link_id = ? ORDER BY s.created_at",
            (link_id,),
        ).fetchall()
        return [self._station_state(r) for r in rows]

    @staticmethod
    def _station_state(row):
        highest, bitmap = row["highest"], row["bitmap"] or 0
        return {
            "id": row["id"],
            "link_id": row["link_id"],
            "name": row["name"],
            "snapshot_id": row["snapshot_id"],
            "highest": highest,
            "bitmap": bitmap,
            "recent": win.recent_positions(highest, bitmap),
        }

    # ---------------------------------------------------------------- frames

    def submit_frame(self, link_id, counter, receipt_id, payload, station_id=None):
        """Atomically resolve a frame arrival at the primary or a secondary.

        ``station_id`` is None for the link's primary station; otherwise the
        frame is resolved against that secondary station's forked window.
        Returns the verdict dict (anchored on the receiving station's window).
        Raises ``LookupError`` for an unknown link/station, ``OriginError`` if
        the station does not belong to the link, and ``ReceiptConflict`` when
        a receipt id is reused with a changed link/counter/payload.  Window
        state and receipt record commit in the same transaction.

        The receipt ledger is global.  Replaying an identical receipt at the
        *other* station still returns the first verdict; the station it first
        arrived at is not part of the conflict key.
        """
        payload_hash = canonical_hash(payload)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if station_id is None:
                    row = self.conn.execute(
                        "SELECT highest, bitmap FROM link_windows "
                        "WHERE link_id = ?",
                        (link_id,),
                    ).fetchone()
                    if row is None:
                        raise LookupError(link_id)
                else:
                    own = self.conn.execute(
                        "SELECT w.highest AS highest, w.bitmap AS bitmap "
                        "FROM stations s JOIN station_windows w "
                        "ON w.station_id = s.id "
                        "WHERE s.id = ? AND s.link_id = ?",
                        (station_id, link_id),
                    ).fetchone()
                    if own is None:
                        # Either the station does not exist or it belongs to a
                        # different link; in both cases no window may change.
                        if self.conn.execute(
                                "SELECT 1 FROM stations WHERE id = ?",
                                (station_id,)).fetchone() is None:
                            raise LookupError(station_id)
                        raise OriginError(station_id)
                    row = own

                prior = self.conn.execute(
                    "SELECT link_id, counter, payload_hash, status, extended "
                    "FROM receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()

                retransmit = prior is not None
                if retransmit:
                    # Stable receipt ids are global: an identical retransmission
                    # (same link, counter and payload, at either station) replays
                    # the first verdict; reusing the id with the link, counter or
                    # payload changed is a conflict -- the station is allowed to
                    # differ.
                    if (prior["link_id"] != link_id
                            or prior["counter"] != counter
                            or prior["payload_hash"] != payload_hash):
                        raise ReceiptConflict(receipt_id)
                    status = prior["status"]
                    extended = prior["extended"]
                    highest = row["highest"]
                    bitmap = row["bitmap"] or 0
                else:
                    status, new_highest, new_bitmap = win.decide(
                        counter, row["highest"], row["bitmap"] or 0
                    )
                    extended, _ = win.extend_counter(counter, row["highest"])
                    if station_id is None:
                        self.conn.execute(
                            "UPDATE link_windows SET highest = ?, bitmap = ? "
                            "WHERE link_id = ?",
                            (new_highest, new_bitmap, link_id),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE station_windows SET highest = ?, bitmap = ? "
                            "WHERE station_id = ?",
                            (new_highest, new_bitmap, station_id),
                        )
                    self.conn.execute(
                        "INSERT INTO receipts (link_id, station_id, receipt_id, "
                        "counter, payload_hash, status, extended, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))",
                        (link_id, station_id, receipt_id, counter, payload_hash,
                         status, extended),
                    )
                    highest, bitmap = new_highest, new_bitmap

                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

        return {
            "status": status,
            "retransmit": retransmit,
            "counter": counter,
            "extended": extended,
            "highest": highest,
            "recent": win.recent_positions(highest, bitmap),
        }

    # ----------------------------------------------------------- converge

    def converge(self, link_id, station_id):
        """Merge the primary and one secondary station after a reconnect.

        Both windows, the receipt ledger and the convergence record are read
        and written inside one ``BEGIN IMMEDIATE`` transaction, so a frame
        being committed at either station can never interleave with the merge.
        Both bitmaps are projected onto the higher highest (their snapshot is
        the common base) and unioned; out-of-window positions are dropped.
        Afterwards both stations hold the identical merged window.

        Returns the merged window plus the positions the other station filled
        in.  Raises ``LookupError`` for an unknown link/station and
        ``OriginError`` for a station that does not belong to the link.
        """
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                primary = self.conn.execute(
                    "SELECT highest, bitmap FROM link_windows WHERE link_id = ?",
                    (link_id,),
                ).fetchone()
                if primary is None:
                    raise LookupError(link_id)

                secondary = self.conn.execute(
                    "SELECT w.highest AS highest, w.bitmap AS bitmap "
                    "FROM stations s JOIN station_windows w "
                    "ON w.station_id = s.id "
                    "WHERE s.id = ? AND s.link_id = ?",
                    (station_id, link_id),
                ).fetchone()
                if secondary is None:
                    if self.conn.execute(
                            "SELECT 1 FROM stations WHERE id = ?",
                            (station_id,)).fetchone() is None:
                        raise LookupError(station_id)
                    raise OriginError(station_id)

                p_h, p_b = primary["highest"], primary["bitmap"] or 0
                s_h, s_b = secondary["highest"], secondary["bitmap"] or 0
                merged_h, merged_b, added_primary, added_secondary = \
                    win.merge_windows(p_h, p_b, s_h, s_b)

                self.conn.execute(
                    "UPDATE link_windows SET highest = ?, bitmap = ? "
                    "WHERE link_id = ?",
                    (merged_h, merged_b, link_id),
                )
                self.conn.execute(
                    "UPDATE station_windows SET highest = ?, bitmap = ? "
                    "WHERE station_id = ?",
                    (merged_h, merged_b, station_id),
                )
                convergence_id = uuid.uuid4().hex
                self.conn.execute(
                    "INSERT INTO convergences (id, link_id, station_id, "
                    "merged_highest, merged_bitmap, added_primary, "
                    "added_secondary, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))",
                    (convergence_id, link_id, station_id, merged_h, merged_b,
                     json.dumps(added_primary), json.dumps(added_secondary)),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

        return {
            "id": convergence_id,
            "station_id": station_id,
            "highest": merged_h,
            "bitmap": merged_b,
            "recent": win.recent_positions(merged_h, merged_b),
            # positions the secondary filled into the primary, and vice versa
            "added_primary": added_primary,
            "added_secondary": added_secondary,
        }
