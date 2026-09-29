"""SQLite persistence: link windows, secondary stations and stable receipts.

Every frame arrival, secondary-station creation and convergence is resolved
inside one ``BEGIN IMMEDIATE`` transaction against the sliding-window state and
the receipt ledger (all serialised through a single write lock), so that
concurrent arrivals/convergence and process restarts always derive consistent
verdicts.
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
CREATE TABLE IF NOT EXISTS stations (
    station_id      TEXT PRIMARY KEY REFERENCES links(id),
    primary_id      TEXT NOT NULL REFERENCES links(id),
    snapshot_highest INTEGER,
    snapshot_bitmap  INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    converged_at    REAL
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id   TEXT PRIMARY KEY,
    link_id      TEXT NOT NULL REFERENCES links(id),
    station_id   TEXT NOT NULL,
    counter      INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL,
    extended     INTEGER NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stations_primary ON stations(primary_id);
"""


class ReceiptConflict(Exception):
    """A stable receipt id was reused with a different link/counter/payload."""


class StationError(Exception):
    """A secondary station cannot be derived from the requested source."""


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
        """Bring older databases up to the station-aware schema."""
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(receipts)")}
        if cols and "station_id" not in cols:
            # Pre-station databases only ever had one station per link, so the
            # link itself is the receiving station of every historical receipt.
            self.conn.execute("ALTER TABLE receipts ADD COLUMN station_id TEXT")
            self.conn.execute("UPDATE receipts SET station_id = link_id")

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

    def _station_meta(self, station_id):
        """Row from ``stations`` for a secondary, else None for a primary."""
        return self.conn.execute(
            "SELECT station_id, primary_id, snapshot_highest, "
            "snapshot_bitmap, converged_at FROM stations WHERE station_id = ?",
            (station_id,),
        ).fetchone()

    def get_link(self, link_id):
        row = self.conn.execute(
            "SELECT l.id, l.name, w.highest, w.bitmap, "
            "s.primary_id AS s_primary, s.snapshot_highest AS s_snap_h, "
            "s.snapshot_bitmap AS s_snap_b, s.converged_at AS s_converged "
            "FROM links l JOIN link_windows w ON w.link_id = l.id "
            "LEFT JOIN stations s ON s.station_id = l.id WHERE l.id = ?",
            (link_id,),
        ).fetchone()
        if row is None:
            return None
        state = self._link_state(row)
        if state["role"] == "primary":
            state["stations"] = self._secondary_states(link_id)
        return state

    def list_links(self):
        rows = self.conn.execute(
            "SELECT l.id, l.name, w.highest, w.bitmap, "
            "s.primary_id AS s_primary, s.snapshot_highest AS s_snap_h, "
            "s.snapshot_bitmap AS s_snap_b, s.converged_at AS s_converged "
            "FROM links l JOIN link_windows w ON w.link_id = l.id "
            "LEFT JOIN stations s ON s.station_id = l.id "
            "ORDER BY l.created_at"
        ).fetchall()
        return [self._link_state(r) for r in rows]

    @staticmethod
    def _link_state(row):
        highest = row["highest"]
        bitmap = row["bitmap"] or 0
        keys = row.keys()
        is_secondary = "s_primary" in keys and row["s_primary"] is not None
        state = {
            "id": row["id"],
            "name": row["name"],
            "highest": highest,
            "bitmap": bitmap,
            "recent": win.recent_positions(highest, bitmap),
            "role": "secondary" if is_secondary else "primary",
        }
        if is_secondary:
            snap_h = row["s_snap_h"]
            snap_b = row["s_snap_b"] or 0
            state["primary_id"] = row["s_primary"]
            state["converged"] = row["s_converged"] is not None
            state["snapshot"] = {
                "highest": snap_h,
                "bitmap": snap_b,
                "recent": win.recent_positions(snap_h, snap_b),
            }
        return state

    def _secondary_states(self, primary_id):
        rows = self.conn.execute(
            "SELECT l.id, l.name, w.highest, w.bitmap, "
            "s.primary_id AS s_primary, s.snapshot_highest AS s_snap_h, "
            "s.snapshot_bitmap AS s_snap_b, s.converged_at AS s_converged "
            "FROM stations s JOIN links l ON l.id = s.station_id "
            "JOIN link_windows w ON w.link_id = s.station_id "
            "WHERE s.primary_id = ? ORDER BY s.created_at",
            (primary_id,),
        ).fetchall()
        states = []
        for r in rows:
            st = self._link_state(r)
            states.append(st)
        return states

    # ------------------------------------------------------------- stations

    def create_station(self, primary_id, name):
        """Create a secondary station from an explicit same-source snapshot.

        The secondary's window is seeded with the primary's current extended
        highest and bitmap, so both stations calibrate their 64-bit extended
        sequence numbers against the exact same basis.  Only a primary station
        can be the snapshot source (a secondary cannot be re-derived from a
        secondary).
        """
        station_id = uuid.uuid4().hex
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                prow = self.conn.execute(
                    "SELECT highest, bitmap FROM link_windows WHERE link_id = ?",
                    (primary_id,),
                ).fetchone()
                if prow is None:
                    raise LookupError(primary_id)
                if self._station_meta(primary_id) is not None:
                    raise StationError(
                        "a secondary station can only be derived from a "
                        "primary station snapshot")

                snap_highest = prow["highest"]
                snap_bitmap = prow["bitmap"] or 0
                self.conn.execute(
                    "INSERT INTO links (id, name, created_at) "
                    "VALUES (?, ?, strftime('%s','now'))",
                    (station_id, name),
                )
                self.conn.execute(
                    "INSERT INTO link_windows (link_id, highest, bitmap) "
                    "VALUES (?, ?, ?)",
                    (station_id, snap_highest, snap_bitmap),
                )
                self.conn.execute(
                    "INSERT INTO stations (station_id, primary_id, "
                    "snapshot_highest, snapshot_bitmap, created_at) "
                    "VALUES (?, ?, ?, ?, strftime('%s','now'))",
                    (station_id, primary_id, snap_highest, snap_bitmap),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.get_link(station_id)

    def converge_station(self, primary_id, secondary_id):
        """Run the one-shot convergence of a secondary back into its primary.

        Both windows are read, merged and written back to *both* stations in a
        single ``BEGIN IMMEDIATE`` transaction, serialised against frame
        submissions and receipt writes through the same lock/transaction
        sequence.  Bitmaps are projected onto the higher extended highest and
        unioned; positions outside the 64-wide window are masked off instead of
        being brought back into the acceptable range.

        Returns a dict with the merged window and the positions contributed by
        the secondary (present there, missing at the primary).  Raises
        ``LookupError`` for unknown links and ``StationError`` for a foreign
        source, a non-secondary target or an already converged snapshot; in
        every error case neither window is modified.
        """
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if self.conn.execute(
                        "SELECT 1 FROM link_windows WHERE link_id = ?",
                        (primary_id,)).fetchone() is None:
                    raise LookupError(primary_id)
                if self.conn.execute(
                        "SELECT 1 FROM link_windows WHERE link_id = ?",
                        (secondary_id,)).fetchone() is None:
                    raise LookupError(secondary_id)

                meta = self._station_meta(secondary_id)
                if meta is None:
                    raise StationError(
                        "convergence target is not a secondary station")
                if meta["primary_id"] != primary_id:
                    # A station from a different source can never alter a
                    # window it does not share a snapshot with.
                    raise StationError(
                        "secondary station derives from a different source")
                if meta["converged_at"] is not None:
                    raise StationError(
                        "snapshot already converged; create a fresh snapshot")

                p_row = self.conn.execute(
                    "SELECT name, highest, bitmap FROM links l "
                    "JOIN link_windows w ON w.link_id = l.id WHERE l.id = ?",
                    (primary_id,)).fetchone()
                s_row = self.conn.execute(
                    "SELECT name, highest, bitmap FROM links l "
                    "JOIN link_windows w ON w.link_id = l.id WHERE l.id = ?",
                    (secondary_id,)).fetchone()

                p_h, p_b = p_row["highest"], p_row["bitmap"] or 0
                s_h, s_b = s_row["highest"], s_row["bitmap"] or 0
                top, merged, added = win.merge_windows(p_h, p_b, s_h, s_b)

                self.conn.execute(
                    "UPDATE link_windows SET highest = ?, bitmap = ? WHERE link_id = ?",
                    (top, merged, primary_id))
                self.conn.execute(
                    "UPDATE link_windows SET highest = ?, bitmap = ? WHERE link_id = ?",
                    (top, merged, secondary_id))
                self.conn.execute(
                    "UPDATE stations SET converged_at = strftime('%s','now') "
                    "WHERE station_id = ?",
                    (secondary_id,))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

        return {
            "highest": top,
            "bitmap": merged,
            "recent": win.recent_positions(top, merged),
            "added": added,
            "primary": {
                "id": primary_id,
                "name": p_row["name"],
                "highest": p_h,
                "recent": win.recent_positions(p_h, p_b),
            },
            "secondary": {
                "id": secondary_id,
                "name": s_row["name"],
                "highest": s_h,
                "recent": win.recent_positions(s_h, s_b),
            },
        }

    # ---------------------------------------------------------------- frames

    def submit_frame(self, station_id, counter, receipt_id, payload):
        """Atomically resolve a frame arrival at a station.

        ``station_id`` is the receiving station (a primary's own link id, or a
        secondary station's link id).  Receipts carry both the receiving
        ``station_id`` and the logical ``link_id`` (the primary lineage): an
        identical retransmission at the *other* station of the same link
        replays the first verdict, while reusing the receipt id on a different
        link/counter/payload is a conflict.  Window state and receipt record
        are committed in the same transaction.  A secondary whose snapshot has
        converged only accepts read-only verdict replays; new frames there are
        rejected with ``StationError`` so the closed window can never change.

        Raises ``LookupError`` for an unknown station, ``StationError`` for a
        new frame at a closed secondary and ``ReceiptConflict`` for a reused id.
        """
        payload_hash = canonical_hash(payload)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT highest, bitmap FROM link_windows WHERE link_id = ?",
                    (station_id,),
                ).fetchone()
                if row is None:
                    raise LookupError(station_id)

                meta = self._station_meta(station_id)
                converged = meta is not None and meta["converged_at"] is not None
                if meta is not None:
                    logical_link = meta["primary_id"]
                else:
                    logical_link = station_id

                prior = self.conn.execute(
                    "SELECT link_id, counter, payload_hash, status, extended "
                    "FROM receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()

                retransmit = prior is not None
                if retransmit:
                    # Stable receipt ids are global: an identical retransmission
                    # (same logical link, counter and payload) replays the first
                    # verdict even when it first arrived at the other station;
                    # reusing the id with the link, counter or payload changed is
                    # a conflict.  A replay never mutates a window, so it is
                    # still served at a secondary whose snapshot has converged.
                    if (prior["link_id"] != logical_link
                            or prior["counter"] != counter
                            or prior["payload_hash"] != payload_hash):
                        raise ReceiptConflict(receipt_id)
                    status = prior["status"]
                    extended = prior["extended"]
                    highest = row["highest"]
                    bitmap = row["bitmap"] or 0
                else:
                    if converged:
                        # A new verdict at a converged secondary is the only
                        # thing that could change a closed snapshot's window;
                        # the episode is over, so refuse before deciding.
                        raise StationError(
                            "secondary station already converged; its snapshot "
                            "episode is closed")
                    status, new_highest, new_bitmap = win.decide(
                        counter, row["highest"], row["bitmap"] or 0
                    )
                    extended, _ = win.extend_counter(counter, row["highest"])
                    self.conn.execute(
                        "UPDATE link_windows SET highest = ?, bitmap = ? WHERE link_id = ?",
                        (new_highest, new_bitmap, station_id),
                    )
                    self.conn.execute(
                        "INSERT INTO receipts (link_id, station_id, receipt_id, "
                        "counter, payload_hash, status, extended, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))",
                        (logical_link, station_id, receipt_id, counter,
                         payload_hash, status, extended),
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
