import os
import tempfile
import unittest

from app.db import Database, ReceiptConflict, StationError
from app import window as win

MOD = 1 << 32
HALF = 1 << 31


# ---------------------------------------------------------------- pure window


class TestProjectBitmap(unittest.TestCase):
    def test_forward_shift_keeps_relative_positions(self):
        # bitmap with positions 10, 9, 8 anchored at highest 10
        bm = 0b111
        self.assertEqual(win.project_bitmap(bm, 10, 12), 0b111 << 2)

    def test_positions_outside_window_are_masked_off(self):
        # highest 10 window 8..10; anchoring at 1000 slides every bit past
        # the 64-wide window -> nothing is carried back into range.
        self.assertEqual(win.project_bitmap(0b111, 10, 1000), 0)

    def test_backward_shift_drops_bits_that_would_run_ahead(self):
        # Re-anchoring to a lower highest moves older bits toward position 0;
        # bits that would land above the new highest shift out at the low end.
        self.assertEqual(win.project_bitmap(0b101, 10, 9), 0b101 >> 1)


class TestMergeWindows(unittest.TestCase):
    def test_disjoint_out_of_order_positions_union(self):
        snap_h, snap_b = 10, 0b111            # 10, 9, 8
        _, h_a, b_a = win.decide(11, snap_h, snap_b)   # primary gets 11
        _, h_b, b_b = win.decide(12, snap_h, snap_b)   # secondary gets 12
        top, merged, added = win.merge_windows(h_a, b_a, h_b, b_b)
        self.assertEqual(top, 12)
        self.assertEqual(win.recent_positions(top, merged),
                         [12, 11, 10, 9, 8])
        # 12 was missing at station A and is filled in by the other station.
        self.assertEqual(added, [12])

    def test_merge_is_symmetric_for_the_resulting_window(self):
        snap_h, snap_b = MOD - 2, 0b111       # MOD-2, MOD-3, MOD-4
        # Primary: MOD-1 then MOD+2 (epoch 0 tail and epoch 1, out of order).
        _, h_a, b_a = win.decide(MOD - 1, snap_h, snap_b)
        _, h_a, b_a = win.decide(2, h_a, b_a)
        # Secondary: counter 0 -> MOD, across the wrap.
        _, h_b, b_b = win.decide(0, snap_h, snap_b)

        top_ab, merged_ab, added_ab = win.merge_windows(h_a, b_a, h_b, b_b)
        top_ba, merged_ba, added_ba = win.merge_windows(h_b, b_b, h_a, b_a)
        self.assertEqual((top_ab, merged_ab), (top_ba, merged_ba))
        self.assertEqual(top_ab, MOD + 2)
        # Union around the wrap: MOD+2, MOD, MOD-1, MOD-2, MOD-3, MOD-4.
        self.assertEqual(
            win.recent_positions(top_ab, merged_ab),
            [MOD + 2, MOD, MOD - 1, MOD - 2, MOD - 3, MOD - 4])
        # Viewed from A: B fills MOD (which A never saw); viewed from B:
        # A fills MOD+2 and MOD-1.
        self.assertEqual(added_ab, [MOD])
        self.assertEqual(added_ba, [MOD + 2, MOD - 1])

    def test_far_ahead_station_discards_other_stations_old_positions(self):
        _, h_a, b_a = win.decide(1000, 10, 0b111)
        # Secondary never advances beyond the snapshot.
        top, merged, added = win.merge_windows(h_a, b_a, 10, 0b111)
        self.assertEqual(top, 1000)
        self.assertEqual(merged, 1)             # old bits fell out, not revived
        self.assertEqual(added, [])

    def test_empty_station_contributes_nothing(self):
        top, merged, added = win.merge_windows(20, 1 << 3, None, 0)
        self.assertEqual((top, merged, added), (20, 1 << 3, []))
        top, merged, added = win.merge_windows(None, 0, 20, 1 << 3)
        self.assertEqual((top, merged), (20, 1 << 3))
        self.assertEqual(added, [17])
        self.assertEqual(win.merge_windows(None, 0, None, 0), (None, 0, []))

    def test_overlapping_bitmaps_do_not_double_count(self):
        # Both stations accepted 30; union must keep a single bit.
        _, h_a, b_a = win.decide(30, 0, 1)
        _, h_b, b_b = win.decide(30, 0, 1)
        _, h_a, b_a = win.decide(31, h_a, b_a)
        _, h_b, b_b = win.decide(29, h_b, b_b)
        top, merged, added = win.merge_windows(h_a, b_a, h_b, b_b)
        self.assertEqual(top, 31)
        self.assertEqual(win.recent_positions(top, merged), [31, 30, 29, 0])


# ------------------------------------------------------------- persistence


class TempDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def make_link(self, name="L"):
        return self.db.create_link(name)["id"]

    def make_station(self, primary, name="sec"):
        return self.db.create_station(primary, name)["id"]

    def submit(self, station, counter, receipt, payload=None):
        return self.db.submit_frame(
            station, counter, receipt,
            payload if payload is not None else {"v": 1})


class TestStationCreation(TempDB):
    def test_station_seeds_from_primary_snapshot(self):
        p = self.make_link()
        self.submit(p, 40, "a")
        self.submit(p, 42, "b")
        s = self.make_station(p)
        primary = self.db.get_link(p)
        secondary = self.db.get_link(s)
        self.assertEqual(primary["role"], "primary")
        self.assertEqual(secondary["role"], "secondary")
        self.assertEqual(secondary["primary_id"], p)
        self.assertFalse(secondary["converged"])
        # Same common basis calibrates both stations' extended seq numbers.
        self.assertEqual(secondary["highest"], primary["highest"])
        self.assertEqual(secondary["bitmap"], primary["bitmap"])
        self.assertEqual(secondary["snapshot"]["highest"], 42)
        self.assertEqual(secondary["snapshot"]["recent"], [42, 40])
        # Primary state lists its secondary stations.
        self.assertEqual([x["id"] for x in primary["stations"]], [s])

    def test_station_requires_known_primary(self):
        with self.assertRaises(LookupError):
            self.db.create_station("missing", "x")

    def test_station_cannot_derive_from_secondary(self):
        p = self.make_link()
        s = self.make_station(p)
        with self.assertRaises(StationError):
            self.db.create_station(s, "tertiary")
        # No link row was left behind.
        self.assertEqual(len(self.db.list_links()), 2)

    def test_snapshot_is_point_in_time(self):
        p = self.make_link()
        self.submit(p, 5, "a")
        s = self.make_station(p)
        self.submit(p, 99, "b")      # primary advances after the snapshot
        secondary = self.db.get_link(s)
        self.assertEqual(secondary["highest"], 5)
        self.assertEqual(secondary["snapshot"]["highest"], 5)

    def test_empty_snapshot_stations_share_epoch_zero_basis(self):
        # A secondary may be created while the primary has never seen a frame;
        # both windows then calibrate first frames at epoch 0 and converge by
        # plain integer alignment (highest=None on both sides).
        p = self.make_link()
        s = self.make_station(p)
        self.submit(p, 12, "p")
        self.submit(s, 10, "s1")
        self.submit(s, 11, "s2")
        result = self.db.converge_station(p, s)
        self.assertEqual(result["highest"], 12)
        self.assertEqual(result["recent"], [12, 11, 10])
        self.assertEqual(result["added"], [11, 10])
        self.assertEqual(
            (self.db.get_link(p)["bitmap"], self.db.get_link(s)["bitmap"]),
            (0b111, 0b111))


class TestDivergenceAndConvergence(TempDB):
    def test_each_station_accepts_during_outage_then_converges(self):
        p = self.make_link()
        for c, r in ((MOD - 3, "a"), (MOD - 2, "b")):
            self.submit(p, c, r)
        s = self.make_station(p)

        # Brief disconnect: different out-of-order frames at each station,
        # including frames on each side of the 32-bit wrap.
        self.submit(p, MOD - 1, "p1")
        self.submit(s, 0, "s1")                  # -> extended MOD
        self.submit(s, 2, "s2")                  # -> extended MOD+2
        self.submit(p, 1, "p2")                  # -> extended MOD+1

        result = self.db.converge_station(p, s)
        self.assertEqual(result["highest"], MOD + 2)
        # Positions filled in by the secondary that the primary lacked: the
        # primary never saw counter 0 (MOD) and lagged behind the secondary's
        # top frame MOD+2.
        self.assertEqual(result["added"], [MOD + 2, MOD])

        # After a refresh both stations expose the exact same window.
        p_state = self.db.get_link(p)
        s_state = self.db.get_link(s)
        self.assertEqual((p_state["highest"], p_state["bitmap"]),
                         (s_state["highest"], s_state["bitmap"]))
        self.assertEqual(p_state["recent"],
                         [MOD + 2, MOD + 1, MOD, MOD - 1, MOD - 2, MOD - 3])
        self.assertTrue(s_state["converged"])

    def test_old_frames_are_not_reaccepted_after_convergence(self):
        p = self.make_link()
        self.submit(p, 10, "a")
        s = self.make_station(p)
        self.submit(p, 12, "b")
        self.submit(s, 11, "c")
        self.db.converge_station(p, s)
        # Every position either station accepted is a duplicate on replay at
        # the still-open primary.
        for c, r in ((10, "x1"), (11, "x2"), (12, "x3")):
            self.assertEqual(self.submit(p, c, r)["status"], "duplicate", c)
        # The converged secondary rejects new verdicts outright.
        for c, r in ((10, "y1"), (11, "y2"), (12, "y3")):
            with self.assertRaises(StationError):
                self.submit(s, c, r)
        # ... but an identical retransmission still replays the first verdict
        # read-only at the closed secondary, changing no window.
        replay = self.submit(s, 11, "c")
        self.assertTrue(replay["retransmit"])
        self.assertEqual(replay["status"], "accepted")
        self.assertEqual(self.db.get_link(s)["bitmap"],
                         self.db.get_link(p)["bitmap"])

    def test_window_external_positions_are_dropped_not_revived(self):
        p = self.make_link()
        self.submit(p, 0, "a")
        s = self.make_station(p)
        self.submit(p, 1000, "b")
        self.submit(s, 1, "s1")                 # far behind primary's top
        result = self.db.converge_station(p, s)
        self.assertEqual(result["highest"], 1000)
        self.assertEqual(result["bitmap"], 1)
        self.assertEqual(result["added"], [])
        # The lagging position must not have re-entered the acceptable window.
        self.assertEqual(self.submit(p, 1, "late")["status"], "expired")

    def test_convergence_is_one_shot(self):
        p = self.make_link()
        s = self.make_station(p)
        self.submit(s, 3, "s")
        self.db.converge_station(p, s)
        with self.assertRaises(StationError):
            self.db.converge_station(p, s)

    def test_secondary_closed_after_convergence(self):
        p = self.make_link()
        self.submit(p, 1, "a")
        s = self.make_station(p)
        self.db.converge_station(p, s)
        with self.assertRaises(StationError):
            self.submit(s, 5, "late")
        # Neither window was touched by the rejected submission.
        self.assertEqual(self.db.get_link(s)["highest"], 1)
        # Primary keeps accepting normally.
        self.assertEqual(self.submit(p, 5, "p")["status"], "accepted")

    def test_foreign_secondary_cannot_change_primary_window(self):
        p1 = self.make_link("one")
        p2 = self.make_link("two")
        self.submit(p1, 1, "a")
        self.submit(p2, 100, "z")
        s_of_p2 = self.make_station(p2)
        self.submit(s_of_p2, 104, "zz")

        before = self.db.get_link(p1)
        with self.assertRaises(StationError):
            self.db.converge_station(p1, s_of_p2)
        with self.assertRaises(StationError):
            self.db.converge_station(p1, p2)   # a primary is not a secondary
        after = self.db.get_link(p1)
        self.assertEqual((after["highest"], after["bitmap"]),
                         (before["highest"], before["bitmap"]))
        # The foreign station is still open and can converge with its own source.
        result = self.db.converge_station(p2, s_of_p2)
        self.assertEqual(result["highest"], 104)

    def test_unknown_station_convergence_errors(self):
        p = self.make_link()
        with self.assertRaises(LookupError):
            self.db.converge_station(p, "missing")
        with self.assertRaises(LookupError):
            self.db.converge_station("missing", p)

    def test_convergence_serialised_against_concurrent_frames(self):
        import threading

        p = self.make_link()
        self.submit(p, 4, "seed")
        s = self.make_station(p)

        # Which counters returned "accepted" at which station; frames reaching
        # the secondary after convergence committed are refused.
        accepted = {"p": set(), "s": set()}
        accepted_lock = threading.Lock()
        converged = threading.Event()
        barrier = threading.Barrier(3)

        def submitter(station, counters, tag):
            barrier.wait()
            for c in counters:
                try:
                    v = self.db.submit_frame(station, c, f"{tag}-{c}", {"v": 1})
                except StationError:
                    continue                    # lost the race with convergence
                if v["status"] == "accepted":
                    with accepted_lock:
                        accepted[tag].add(c)

        def converger():
            barrier.wait()
            while not converged.is_set():
                try:
                    self.db.converge_station(p, s)
                    converged.set()
                    return
                except StationError:
                    converged.set()
                    return

        threads = [
            threading.Thread(target=submitter, args=(p, range(5, 25), "p")),
            threading.Thread(target=submitter, args=(s, range(25, 45), "s")),
            threading.Thread(target=converger),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        s_state = self.db.get_link(s)
        if not s_state["converged"]:
            # The converger thread can only fail to converge if it never got
            # scheduled ahead of the submitters without raising; normalise by
            # converging once here (the secondary must then be untouched).
            self.db.converge_station(p, s)

        p_final = self.db.get_link(p)
        s_final = self.db.get_link(s)
        self.assertTrue(s_final["converged"])

        # Serialisation invariant: every frame any station committed as
        # accepted must survive in the final primary window, and the highest
        # must be the greatest accepted counter.  The 4..44 span is only 40
        # wide, so nothing falls outside the 64 positions.  No committed frame
        # submission may have been silently overwritten by convergence.
        all_accepted = accepted["p"] | accepted["s"] | {4}
        self.assertTrue(all_accepted)
        self.assertEqual(p_final["highest"], max(all_accepted))
        expected_bm = 0
        for c in all_accepted:
            self.assertLess(p_final["highest"] - c, win.WINDOW_SIZE)
            expected_bm |= 1 << (p_final["highest"] - c)
        self.assertEqual(p_final["bitmap"], expected_bm)
        # The converged secondary never runs past the primary's merged top.
        self.assertLessEqual(s_final["highest"], p_final["highest"])


class TestCrossStationReceipts(TempDB):
    def test_retransmission_at_other_station_replays_first_verdict(self):
        p = self.make_link()
        s = self.make_station(p)
        first = self.submit(p, 7, "rx-1")
        self.assertEqual(first["status"], "accepted")
        replay = self.submit(s, 7, "rx-1")
        self.assertTrue(replay["retransmit"])
        self.assertEqual(replay["status"], "accepted")
        self.assertEqual(replay["extended"], first["extended"])

    def test_expired_verdict_replayed_across_stations(self):
        p = self.make_link()
        s = self.make_station(p)
        self.submit(p, 100, "a")
        verdict = self.submit(p, 0, "old")
        self.assertEqual(verdict["status"], "expired")
        replay = self.submit(s, 0, "old")
        self.assertTrue(replay["retransmit"])
        self.assertEqual(replay["status"], "expired")

    def test_first_verdict_at_secondary_replays_at_primary(self):
        p = self.make_link()
        s = self.make_station(p)
        self.submit(s, 9, "only-sec")
        replay = self.submit(p, 9, "only-sec")
        self.assertTrue(replay["retransmit"])
        self.assertEqual(replay["status"], "accepted")
        # Convergence still fills the primary bitmap from the secondary.
        result = self.db.converge_station(p, s)
        self.assertEqual(result["added"], [9])

    def test_receipt_reuse_changed_payload_across_stations_rejected(self):
        p = self.make_link()
        s = self.make_station(p)
        self.submit(p, 1, "rid", {"v": 1})
        with self.assertRaises(ReceiptConflict):
            self.submit(s, 1, "rid", {"v": 2})

    def test_receipt_reuse_changed_counter_across_stations_rejected(self):
        p = self.make_link()
        s = self.make_station(p)
        self.submit(p, 1, "rid")
        with self.assertRaises(ReceiptConflict):
            self.submit(s, 2, "rid")

    def test_receipt_reuse_on_unrelated_link_rejected(self):
        p1 = self.make_link("one")
        p2 = self.make_link("two")
        self.submit(p1, 1, "global")
        with self.assertRaises(ReceiptConflict):
            self.submit(p2, 1, "global")
        s2 = self.make_station(p2)
        with self.assertRaises(ReceiptConflict):
            self.submit(s2, 1, "global")

    def test_conflict_at_secondary_changes_nothing(self):
        p = self.make_link()
        self.submit(p, 10, "s1")
        s = self.make_station(p)
        with self.assertRaises(ReceiptConflict):
            self.submit(s, 11, "s1")
        self.assertEqual(self.db.get_link(s)["highest"], 10)


class TestRestartAndBackwardsCompat(TempDB):
    def test_stations_and_convergence_survive_reopen(self):
        p = self.make_link()
        self.submit(p, MOD - 1, "a")
        s = self.make_station(p)
        self.submit(s, 0, "b")
        self.db.converge_station(p, s)
        self.db.close()

        db2 = Database(self.db.path)
        try:
            p_state = db2.get_link(p)
            s_state = db2.get_link(s)
            self.assertEqual(p_state["highest"], MOD)
            self.assertEqual((p_state["highest"], p_state["bitmap"]),
                             (s_state["highest"], s_state["bitmap"]))
            self.assertTrue(s_state["converged"])
            self.assertEqual(s_state["snapshot"]["highest"], MOD - 1)
            # Cross-station replay still derives from the persisted ledger.
            replay = db2.submit_frame(s, 0, "b", {"v": 1})
            self.assertTrue(replay["retransmit"])
            self.assertEqual(replay["status"], "accepted")
            with self.assertRaises(StationError):
                db2.submit_frame(s, 1, "c", {"v": 1})
        finally:
            db2.close()

    def test_pre_station_database_is_migrated(self):
        from app.db import canonical_hash
        db_path = os.path.join(self.tmp.name, "legacy.db")
        import sqlite3
        legacy = sqlite3.connect(db_path)
        legacy.executescript("""
            CREATE TABLE links (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                                created_at REAL NOT NULL);
            CREATE TABLE link_windows (link_id TEXT PRIMARY KEY, highest INTEGER,
                                       bitmap INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE receipts (receipt_id TEXT PRIMARY KEY, link_id TEXT NOT NULL,
                counter INTEGER NOT NULL, payload_hash TEXT NOT NULL,
                status TEXT NOT NULL, extended INTEGER NOT NULL, created_at REAL NOT NULL);
            INSERT INTO links VALUES ('L', 'old', 0);
            INSERT INTO link_windows VALUES ('L', 5, 1);
        """)
        legacy.execute(
            "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, 0)",
            ("r1", "L", 5, canonical_hash({"v": 1}), "accepted", 5))
        legacy.commit()
        legacy.close()

        migrated = Database(db_path)
        try:
            state = migrated.get_link("L")
            self.assertEqual(state["role"], "primary")
            self.assertEqual(state["highest"], 5)
            # Historical receipts still replay; they were received by the link
            # itself, which is its own primary station.
            replay = migrated.submit_frame("L", 5, "r1", {"v": 1})
            self.assertTrue(replay["retransmit"])
            self.assertEqual(replay["status"], "accepted")
            # New station behaviour is available on the migrated database.
            sid = migrated.create_station("L", "sec")["id"]
            self.assertEqual(migrated.get_link(sid)["snapshot"]["highest"], 5)
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
