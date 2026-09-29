import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

from app.server import Server

MOD = 1 << 32
HALF = 1 << 31


def _request(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "http.db")
        cls.server = Server(("127.0.0.1", 0), db_path, log_file=open(os.devnull, "w"))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        # Wait for readiness.
        for _ in range(50):
            try:
                _request("GET", cls.base + "/healthz")
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.db.close()
        cls.tmp.cleanup()

    def test_health(self):
        code, body = _request("GET", self.base + "/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            self.assertIn(b"\xe6\xb7\xb1\xe7\xa9\xba", resp.read())  # 深空

    def test_full_link_and_frame_flow(self):
        code, link = _request("POST", self.base + "/api/links", {"name": "flow"})
        self.assertEqual(code, 201)
        lid = link["id"]

        code, listing = _request("GET", self.base + "/api/links")
        self.assertEqual(code, 200)
        self.assertTrue(any(l["id"] == lid for l in listing["links"]))

        def frame(counter, rid, payload=None):
            return _request("POST", f"{self.base}/api/links/{lid}/frames",
                            {"counter": counter, "receipt_id": rid,
                             "payload": payload if payload is not None else {"v": 1}})

        code, v = frame(MOD - 1, "w1")
        self.assertEqual(code, 200)
        self.assertEqual(v["status"], "accepted")

        code, v = frame(0, "w2")                 # wraps to epoch 1
        self.assertEqual(v["status"], "accepted")
        self.assertEqual(v["extended"], MOD)

        code, v = frame(0, "w2")                 # identical replay
        self.assertEqual(v["status"], "accepted")
        self.assertTrue(v["retransmit"])

        code, v = frame(0, "w2", {"v": 9})       # payload changed
        self.assertEqual(code, 409)

        code, v = frame(0, "w2")                 # ledger unaffected by conflict
        self.assertEqual(code, 200)
        self.assertTrue(v["retransmit"])

        code, v = frame(0, "w2-on-other-link")  # sanity: other id fine here
        self.assertEqual(code, 200)
        self.assertEqual(v["status"], "duplicate")

    def test_unknown_link_404(self):
        code, _ = _request("POST",
                           f"{self.base}/api/links/nope/frames",
                           {"counter": 1, "receipt_id": "x", "payload": {}})
        self.assertEqual(code, 404)

    def test_bad_counter_400(self):
        _, link = _request("POST", self.base + "/api/links", {"name": "b"})
        lid = link["id"]
        for bad in (-1, MOD, "abc", None):
            code, body = _request(
                "POST", f"{self.base}/api/links/{lid}/frames",
                {"counter": bad, "receipt_id": "r", "payload": {}})
            self.assertEqual(code, 400, bad)

    def test_equidistant_counter_rejected(self):
        _, link = _request("POST", self.base + "/api/links", {"name": "tie"})
        lid = link["id"]
        _request("POST", f"{self.base}/api/links/{lid}/frames",
                 {"counter": 0, "receipt_id": "z0", "payload": {}})
        code, v = _request("POST", f"{self.base}/api/links/{lid}/frames",
                           {"counter": HALF, "receipt_id": "zh", "payload": {}})
        self.assertEqual(code, 200)
        self.assertEqual(v["status"], "rejected")

    # -------------------------------------------------------- secondary flow

    def _new_link(self, name="sec-flow"):
        return _request("POST", self.base + "/api/links", {"name": name})[1]["id"]

    def _frame(self, url, counter, rid, payload=None):
        return _request("POST", url,
                        {"counter": counter, "receipt_id": rid,
                         "payload": payload if payload is not None else {"v": 1}})

    def test_snapshot_fork_secondary_frames_and_converge(self):
        lid = self._new_link()
        p_url = f"{self.base}/api/links/{lid}/frames"
        self._frame(p_url, 0, "p0")

        code, snap = _request("POST",
                              f"{self.base}/api/links/{lid}/snapshots", {})
        self.assertEqual(code, 201)
        self.assertEqual(snap["highest"], 0)

        code, st = _request("POST", f"{self.base}/api/links/{lid}/stations",
                            {"snapshot_id": snap["id"], "name": "backup"})
        self.assertEqual(code, 201)
        sid = st["id"]
        self.assertEqual(st["highest"], 0)

        # station is listed on the link
        code, link = _request("GET", f"{self.base}/api/links/{lid}")
        self.assertEqual([s["id"] for s in link["stations"]], [sid])

        s_url = f"{self.base}/api/links/{lid}/stations/{sid}/frames"
        # Brief disconnect: the two stations accept disjoint frames.
        self._frame(p_url, 2, "p2")
        self._frame(s_url, 1, "s1")
        self._frame(s_url, 3, "s3")
        self._frame(p_url, 0, "p0")                # retransmit replays verdict
        code, v = self._frame(s_url, 0, "p0")     # same receipt at other station
        self.assertEqual(code, 200)
        self.assertTrue(v["retransmit"])

        code, conv = _request(
            "POST", f"{self.base}/api/links/{lid}/stations/{sid}/converge", {})
        self.assertEqual(code, 200)
        self.assertEqual(conv["highest"], 3)
        self.assertEqual(conv["bitmap"], 0b1111)
        code, link = _request("GET", f"{self.base}/api/links/{lid}")
        code, station = _request("GET", f"{self.base}/api/stations/{sid}")
        # Both refreshed views report the same merged window.
        self.assertEqual((link["highest"], link["bitmap"]),
                         (station["highest"], station["bitmap"]))
        self.assertEqual(link["highest"], conv["highest"])
        self.assertEqual(conv["added_primary"], [3, 1])
        self.assertEqual(conv["added_secondary"], [2])

    def test_foreign_snapshot_fork_is_conflict(self):
        l1 = self._new_link("own")
        l2 = self._new_link("other")
        _, snap = _request("POST", f"{self.base}/api/links/{l1}/snapshots", {})
        code, body = _request("POST",
                              f"{self.base}/api/links/{l2}/stations",
                              {"snapshot_id": snap["id"]})
        self.assertEqual(code, 409)
        self.assertIn("foreign origin", body["error"])
        _, l2state = _request("GET", f"{self.base}/api/links/{l2}")
        self.assertEqual(l2state["stations"], [])

    def test_unknown_snapshot_and_station_are_404(self):
        lid = self._new_link()
        code, _ = _request("POST", f"{self.base}/api/links/{lid}/stations",
                           {"snapshot_id": "nope"})
        self.assertEqual(code, 404)
        code, _ = self._frame(
            f"{self.base}/api/links/{lid}/stations/nope/frames", 1, "x")
        self.assertEqual(code, 404)
        code, _ = _request(
            "POST", f"{self.base}/api/links/{lid}/stations/nope/converge", {})
        self.assertEqual(code, 404)

    def test_foreign_station_endpoints_do_not_change_window(self):
        l1 = self._new_link("a")
        l2 = self._new_link("b")
        self._frame(f"{self.base}/api/links/{l1}/frames", 0, "base")
        _, snap = _request("POST", f"{self.base}/api/links/{l1}/snapshots", {})
        _, st = _request("POST", f"{self.base}/api/links/{l1}/stations",
                         {"snapshot_id": snap["id"]})
        sid = st["id"]
        # Offering l1's station on l2 is a foreign-origin conflict.
        code, _ = self._frame(
            f"{self.base}/api/links/{l2}/stations/{sid}/frames", 1, "y")
        self.assertEqual(code, 409)
        code, _ = _request(
            "POST", f"{self.base}/api/links/{l2}/stations/{sid}/converge", {})
        self.assertEqual(code, 409)
        _, l2state = _request("GET", f"{self.base}/api/links/{l2}")
        self.assertIsNone(l2state["highest"])

    def test_receipt_replay_and_conflict_across_stations(self):
        lid = self._new_link()
        p_url = f"{self.base}/api/links/{lid}/frames"
        self._frame(p_url, 5, "shared", {"v": 1})
        _, snap = _request("POST", f"{self.base}/api/links/{lid}/snapshots", {})
        _, st = _request("POST", f"{self.base}/api/links/{lid}/stations",
                         {"snapshot_id": snap["id"]})
        s_url = f"{self.base}/api/links/{lid}/stations/{st['id']}/frames"
        # identical receipt at the other station replays the first verdict
        code, v = self._frame(s_url, 5, "shared", {"v": 1})
        self.assertEqual(code, 200)
        self.assertTrue(v["retransmit"])
        self.assertEqual(v["status"], "accepted")
        # changed payload at the other station is still a conflict
        code, body = self._frame(s_url, 5, "shared", {"v": 2})
        self.assertEqual(code, 409)
        self.assertIn("receipt id reused", body["error"])


if __name__ == "__main__":
    unittest.main()
