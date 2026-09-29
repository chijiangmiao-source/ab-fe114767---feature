import os
import tempfile
import threading
import unittest

from app.db import Database, OriginError, ReceiptConflict
from app import window as win

MOD = 1 << 32


class StationDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "station.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def link(self, name="L"):
        return self.db.create_link(name)["id"]

    def submit(self, link, counter, rid, payload=None, station=None):
        return self.db.submit_frame(
            link, counter, rid,
            payload if payload is not None else {"v": 1},
            station_id=station)

    def fork(self, link, name="sec", snap=None):
        if snap is None:
            snap = self.db.create_snapshot(link)["id"]
        return self.db.create_station(link, name, snap)["id"]

    def windows(self, link, station):
        p = self.db.get_link(link)
        s = self.db.get_station(station)
        return (p["highest"], p["bitmap"]), (s["highest"], s["bitmap"])


class TestFork(StationDB):
    def test_station_inherits_snapshot_base(self):
        lid = self.link()
        for c, r in zip((10, 11, 12), ("a", "b", "c")):
            self.submit(lid, c, r)
        snapshot = self.db.create_snapshot(lid)
        sid = self.db.create_station(lid, "backup", snapshot["id"])
        self.assertEqual(sid["highest"], 12)
        self.assertEqual(sid["bitmap"], 0b111)
        self.assertEqual(sid["snapshot_id"], snapshot["id"])

    def test_snapshot_is_immutable_when_primary_moves_on(self):
        lid = self.link()
        self.submit(lid, 5, "a")
        snap = self.db.create_snapshot(lid)
        self.submit(lid, 6, "b")                  # primary advances
        frozen = self.db.get_snapshot(snap["id"])
        self.assertEqual(frozen["highest"], 5)    # snapshot unchanged

    def test_foreign_snapshot_cannot_fork_station(self):
        l1, l2 = self.link("one"), self.link("two")
        foreign = self.db.create_snapshot(l1)["id"]
        with self.assertRaises(OriginError):
            self.db.create_station(l2, "rogue", foreign)
        # No station and no window were created for the foreign origin.
        self.assertEqual(self.db.list_stations(l2), [])

    def test_unknown_snapshot_is_lookup_error(self):
        lid = self.link()
        with self.assertRaises(LookupError):
            self.db.create_station(lid, "sec", "deadbeef")

    def test_empty_window_snapshot_is_a_valid_common_base(self):
        lid = self.link()
        sid = self.fork(lid)
        self.assertIsNone(self.db.get_station(sid)["highest"])


class TestSecondaryFrames(StationDB):
    def test_secondary_frame_does_not_touch_primary(self):
        lid = self.link()
        self.submit(lid, 0, "p0")
        sid = self.fork(lid)
        v = self.submit(lid, 5, "s5", station=sid)
        self.assertEqual(v["status"], "accepted")
        self.assertEqual(v["highest"], 5)
        primary = self.db.get_link(lid)
        self.assertEqual(primary["highest"], 0)   # primary untouched

    def test_station_from_other_link_rejected_without_change(self):
        l1, l2 = self.link("one"), self.link("two")
        self.submit(l1, 0, "x")
        sid = self.fork(l1)
        before = self.db.get_link(l1)
        with self.assertRaises(OriginError):
            self.submit(l2, 1, "y", station=sid)
        self.assertEqual(
            (self.db.get_link(l1)["highest"], self.db.get_link(l1)["bitmap"]),
            (before["highest"], before["bitmap"]))
        self.assertIsNone(self.db.get_link(l2)["highest"])

    def test_unknown_station_is_lookup_error(self):
        lid = self.link()
        with self.assertRaises(LookupError):
            self.submit(lid, 1, "z", station="nope")


class TestConverge(StationDB):
    def test_converge_unions_gaps_and_aligns_both_windows(self):
        lid = self.link()
        self.submit(lid, 0, "p0")
        sid = self.fork(lid)
        # Disconnect: primary and secondary each receive different frames.
        self.submit(lid, 2, "p2")                 # primary: 0,2
        self.submit(lid, 1, "s1", station=sid)    # secondary: 0,1
        self.submit(lid, 3, "s3", station=sid)    # secondary highest runs on

        result = self.db.converge(lid, sid)
        self.assertEqual(result["highest"], 3)
        self.assertEqual(result["bitmap"], 0b1111)
        # secondary filled primary's gap at 1 and pulled it forward to 3
        self.assertEqual(result["added_primary"], [3, 1])
        # primary filled secondary's gap at 2
        self.assertEqual(result["added_secondary"], [2])

        (p_h, p_b), (s_h, s_b) = self.windows(lid, sid)
        self.assertEqual((p_h, p_b), (s_h, s_b))  # identical after refresh
        self.assertEqual((p_h, p_b), (3, 0b1111))

    def test_converge_is_idempotent(self):
        lid = self.link()
        self.submit(lid, 0, "p0")
        sid = self.fork(lid)
        self.submit(lid, 2, "p2")
        self.submit(lid, 1, "s1", station=sid)
        first = self.db.converge(lid, sid)
        second = self.db.converge(lid, sid)
        self.assertEqual(second["highest"], first["highest"])
        self.assertEqual(second["bitmap"], first["bitmap"])
        self.assertEqual(second["added_primary"], [])
        self.assertEqual(second["added_secondary"], [])

    def test_wrap_around_disjoint_out_of_order_then_converge(self):
        lid = self.link()
        # Shared base straddles the wrap.
        for c, r in ((MOD - 2, "b-2"), (MOD - 1, "b-1")):
            self.submit(lid, c, r)
        sid = self.fork(lid)

        # Primary accepts epoch-1 frames 0 and 2 ...
        self.submit(lid, 0, "p0")
        self.submit(lid, 2, "p2")
        # ... while the secondary accepts 1 and 3, some out of order, and it
        # also re-wraps relative to the same common base.
        self.submit(lid, 3, "s3", station=sid)
        self.submit(lid, 1, "s1", station=sid)

        self.db.converge(lid, sid)
        (p_h, p_b), (s_h, s_b) = self.windows(lid, sid)
        self.assertEqual((p_h, p_b), (s_h, s_b))
        self.assertEqual(p_h, MOD + 3)
        # Continuous MOD-2 .. MOD+3 -> six low bits.
        self.assertEqual(p_b, 0b111111)
        # Refreshed views agree and expose the same recent positions.
        self.assertEqual(self.db.get_link(lid)["recent"],
                         self.db.get_station(sid)["recent"])
        # Every one of the six frames is now a duplicate on both stations.
        for c in (MOD - 2, MOD - 1, 0, 1, 2, 3):
            self.assertEqual(
                self.submit(lid, c, f"dup-p-{c}", station=None)["status"],
                "duplicate", ("primary", c))
            self.assertEqual(
                self.submit(lid, c, f"dup-s-{c}", station=sid)["status"],
                "duplicate", ("secondary", c))

    def test_old_frame_cannot_reenter_after_converge(self):
        lid = self.link()
        self.submit(lid, 0, "p0")
        sid = self.fork(lid)
        self.submit(lid, 100, "p100")              # primary jumps ahead
        self.submit(lid, 1, "s1", station=sid)     # secondary lags
        self.db.converge(lid, sid)                 # highest becomes 100
        # Frame 1 projected 99 back: dropped from the merged window, and a
        # fresh arrival at either station is expired, never re-accepted.
        self.assertEqual(
            self.submit(lid, 1, "late-p", station=None)["status"], "expired")
        self.assertEqual(
            self.submit(lid, 1, "late-s", station=sid)["status"], "expired")

    def test_out_of_window_side_is_dropped_not_pulled_back(self):
        lid = self.link()
        self.submit(lid, 0, "p0")
        sid = self.fork(lid)
        self.submit(lid, 100, "p100")              # primary 100 frames ahead
        # secondary only has the base; on merge its bit 0 shifts out.
        result = self.db.converge(lid, sid)
        self.assertEqual(result["highest"], 100)
        self.assertEqual(result["bitmap"], 1)
        self.assertEqual(result["added_primary"], [])

    def test_foreign_station_converge_rejected(self):
        l1, l2 = self.link("one"), self.link("two")
        self.submit(l1, 0, "x")
        sid = self.fork(l1)
        with self.assertRaises(OriginError):
            self.db.converge(l2, sid)
        self.assertIsNone(self.db.get_link(l2)["highest"])

    def test_convergence_recorded_in_same_transaction_series(self):
        lid = self.link()
        self.submit(lid, 0, "p0")
        sid = self.fork(lid)
        self.submit(lid, 1, "s1", station=sid)
        result = self.db.converge(lid, sid)
        row = self.db.conn.execute(
            "SELECT merged_highest, merged_bitmap, added_primary, "
            "added_secondary FROM convergences WHERE id = ?",
            (result["id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["merged_highest"], result["highest"])
        self.assertEqual(row["merged_bitmap"], result["bitmap"])


class TestCrossStationReceipts(StationDB):
    def test_identical_receipt_replayed_at_other_station(self):
        lid = self.link()
        self.submit(lid, 7, "shared")
        sid = self.fork(lid)
        # Same receipt id + counter + payload first seen at the secondary:
        # replay returns the first verdict recorded at the primary.
        replay = self.submit(lid, 7, "shared", station=sid)
        self.assertTrue(replay["retransmit"])
        self.assertEqual(replay["status"], "accepted")

    def test_first_verdict_at_secondary_replayed_at_primary(self):
        lid = self.link()
        self.submit(lid, 0, "base")
        sid = self.fork(lid)
        first = self.submit(lid, 9, "only-sec", station=sid)
        self.assertEqual(first["status"], "accepted")
        replay = self.submit(lid, 9, "only-sec")      # now at primary
        self.assertTrue(replay["retransmit"])
        self.assertEqual(replay["status"], "accepted")

    def test_receipt_reuse_changed_payload_rejected_at_either_station(self):
        lid = self.link()
        self.submit(lid, 1, "rid", {"v": 1})
        sid = self.fork(lid)
        with self.assertRaises(ReceiptConflict):
            self.submit(lid, 1, "rid", {"v": 2}, station=sid)
        with self.assertRaises(ReceiptConflict):
            self.submit(lid, 2, "rid", station=sid)
        # Windows untouched by the conflicts.
        (p_h, _), (s_h, _) = self.windows(lid, sid)
        self.assertEqual(p_h, 1)
        self.assertEqual(s_h, 1)

    def test_receipt_reuse_on_other_link_rejected_even_via_station(self):
        l1, l2 = self.link("one"), self.link("two")
        self.submit(l1, 1, "global")
        s2 = self.fork(l2)
        with self.assertRaises(ReceiptConflict):
            self.submit(l2, 1, "global", station=s2)

    def test_expired_verdict_replayed_across_stations(self):
        lid = self.link()
        self.submit(lid, 100, "far")
        sid = self.fork(lid)
        # An expired frame first seen at the secondary keeps its verdict.
        exp = self.submit(lid, 0, "old", station=sid)
        self.assertEqual(exp["status"], "expired")
        replay = self.submit(lid, 0, "old")
        self.assertTrue(replay["retransmit"])
        self.assertEqual(replay["status"], "expired")


class TestConcurrencyAndRestart(StationDB):
    def test_frames_at_both_stations_serialise_with_converge(self):
        lid = self.link()
        self.submit(lid, 0, "base")
        sid = self.fork(lid)
        errors = []

        def primary_work():
            try:
                for c in range(1, 30, 2):
                    self.submit(lid, c, f"p-{c}")
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        def secondary_work():
            try:
                for c in range(2, 30, 2):
                    self.submit(lid, c, f"s-{c}", station=sid)
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        def converge_work():
            try:
                for _ in range(5):
                    self.db.converge(lid, sid)
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        threads = [threading.Thread(target=primary_work),
                   threading.Thread(target=secondary_work),
                   threading.Thread(target=converge_work)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        # Final convergence of whatever each station last held.
        self.db.converge(lid, sid)
        p = self.db.get_link(lid)
        s = self.db.get_station(sid)
        self.assertEqual((p["highest"], p["bitmap"]),
                         (s["highest"], s["bitmap"]))
        self.assertEqual(p["highest"], 29)
        self.assertEqual(p["bitmap"], (1 << 30) - 1)   # 0..29 contiguous

    def test_station_state_survives_reopen(self):
        lid = self.link()
        self.submit(lid, MOD - 1, "p-edge")        # base at wrap boundary
        sid = self.fork(lid)
        self.submit(lid, 0, "wrap", station=sid)   # secondary wraps to MOD
        self.db.close()

        db2 = Database(self.db.path)
        try:
            station = db2.get_station(sid)
            self.assertEqual(station["highest"], MOD)
            # cross-station receipt replay still works after restart
            v = db2.submit_frame(lid, 0, "wrap", {"v": 1})
            self.assertTrue(v["retransmit"])
            self.assertEqual(v["status"], "accepted")
            result = db2.converge(lid, sid)
            self.assertEqual(result["highest"], MOD)
        finally:
            db2.close()


if __name__ == "__main__":
    unittest.main()
