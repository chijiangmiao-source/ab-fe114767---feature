#!/usr/bin/env python3
"""End-to-end HTTP smoke test run by the Compose ``verify`` service.

Asserts wrap-around, expiry, duplicate, epoch-tie rejection and receipt
semantics against a fully running server, and exits non-zero on any failure.
"""

import json
import sys
import time
import urllib.error
import urllib.request

MOD = 1 << 32
HALF = 1 << 31


def request(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok - {msg}")


def main(base):
    for _ in range(60):
        try:
            code, body = request("GET", base + "/healthz")
            if code == 200 and body.get("status") == "ok":
                break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError("server never became healthy")
    print("ok - healthz reports ok")

    _, link = request("POST", base + "/api/links", {"name": "smoke-wrap"})
    lid = link["id"]
    frames_url = f"{base}/api/links/{lid}/frames"

    def frame(counter, rid, payload=None):
        return request("POST", frames_url,
                       {"counter": counter, "receipt_id": rid,
                        "payload": payload if payload is not None else {"v": 1}})

    # --- wrap-around with out-of-order delivery, each accepted once ---
    code, v = frame(MOD - 2, "s-2")
    check(code == 200 and v["status"] == "accepted", f"counter 2^32-2 accepted ({v})")
    _, v = frame(MOD - 1, "s-1")
    check(v["status"] == "accepted" and v["extended"] == MOD - 1, "2^32-1 accepted")
    _, v = frame(1, "s1")  # arrives before 0
    check(v["status"] == "accepted" and v["extended"] == MOD + 1,
          "out-of-order counter 1 placed in epoch 1")
    _, v = frame(0, "s0")
    check(v["status"] == "accepted" and v["extended"] == MOD,
          "counter 0 wraps to 2^32")

    _, state = request("GET", f"{base}/api/links/{lid}")
    check(state["highest"] == MOD + 1, f"highest extended seq is 2^32+1 (got {state['highest']})")
    check(state["recent"][:4] == [MOD + 1, MOD, MOD - 1, MOD - 2],
          "recent positions show all four wrap frames once")

    # --- duplicates: same counter, new receipt id ---
    _, v = frame(0, "s0-newid")
    check(v["status"] == "duplicate", "counter 0 resent is duplicate")

    # --- identical retransmission replays the FIRST verdict ---
    _, v = frame(0, "s0")
    check(v["status"] == "accepted" and v["retransmit"],
          "identical receipt+payload replays original accepted verdict")

    # --- receipt id reuse with changed counter / payload / link -> 409 ---
    code, _ = frame(5, "s0")
    check(code == 409, "receipt id reused with different counter -> 409")
    code, _ = frame(0, "s0", {"v": 2})
    check(code == 409, "receipt id reused with changed payload -> 409")

    _, other = request("POST", base + "/api/links", {"name": "smoke-other"})
    code, _ = request("POST", f"{base}/api/links/{other['id']}/frames",
                      {"counter": 0, "receipt_id": "s0", "payload": {"v": 1}})
    check(code == 409, "receipt id reused on another link -> 409")

    # --- expired: slide forward, old frame can never re-enter ---
    _, v = frame((MOD + 70) & (MOD - 1), "s70")
    check(v["status"] == "accepted", "jump 70 frames into epoch 1 accepted")
    _, v = frame(MOD - 2, "s-2-late")  # distance 72 from highest
    check(v["status"] == "expired", "old epoch-0 frame after wrap is expired")
    _, v = frame(MOD - 2, "s-2-late2")
    check(v["status"] == "expired", "expired frame stays expired")

    # --- epoch tie: counter exactly 2^31 away is rejected ---
    _, tie = request("POST", base + "/api/links", {"name": "smoke-tie"})
    request("POST", f"{base}/api/links/{tie['id']}/frames",
            {"counter": 0, "receipt_id": "t0", "payload": {}})
    code, v = request("POST", f"{base}/api/links/{tie['id']}/frames",
                      {"counter": HALF, "receipt_id": "th", "payload": {}})
    check(code == 200 and v["status"] == "rejected",
          "equidistant epoch candidates (2^31) rejected")

    # --- secondary station: fork, disjoint wrap frames, converge ---
    _, sec = request("POST", base + "/api/links", {"name": "smoke-secondary"})
    sec_id = sec["id"]
    sec_frames = f"{base}/api/links/{sec_id}/frames"

    def sec_frame(counter, rid, payload=None):
        return request("POST", sec_frames,
                       {"counter": counter, "receipt_id": rid,
                        "payload": payload if payload is not None else {"v": 1}})

    # Common base straddles the wrap on the primary.
    _, v = sec_frame(MOD - 2, "sec-b-2")
    check(v["status"] == "accepted", "secondary-link base frame 2^32-2")
    _, v = sec_frame(MOD - 1, "sec-b-1")
    check(v["status"] == "accepted" and v["highest"] == MOD - 1,
          "secondary-link base frame 2^32-1")

    # The secondary can only be forked from an explicit same-origin snapshot.
    code, snap = request("POST", f"{base}/api/links/{sec_id}/snapshots", {})
    check(code == 201 and snap["highest"] == MOD - 1, "same-origin snapshot frozen")
    code, station = request("POST", f"{base}/api/links/{sec_id}/stations",
                            {"snapshot_id": snap["id"], "name": "backup"})
    check(code == 201 and station["highest"] == MOD - 1,
          "secondary station inherits snapshot base")
    station_id = station["id"]
    st_frames = (f"{base}/api/links/{sec_id}/stations/"
                 f"{station_id}/frames")

    def st_frame(counter, rid, payload=None):
        return request("POST", st_frames,
                       {"counter": counter, "receipt_id": rid,
                        "payload": payload if payload is not None else {"v": 1}})

    # Brief disconnect: the two stations accept different, partly out-of-order
    # frames on either side of the wrap.
    _, v = sec_frame(0, "sec-p0")                   # primary wraps to 2^32
    check(v["status"] == "accepted" and v["extended"] == MOD, "primary sees epoch-1 frame 0")
    _, v = sec_frame(2, "sec-p2")
    check(v["status"] == "accepted", "primary sees frame 2")
    _, v = st_frame(3, "sec-s3")                    # secondary jumps ahead first
    check(v["status"] == "accepted" and v["extended"] == MOD + 3,
          "secondary places frame 3 in epoch 1")
    _, v = st_frame(1, "sec-s1")                    # then the late frame 1
    check(v["status"] == "accepted", "secondary fills out-of-order frame 1")

    # The same receipt retransmitted at the OTHER station replays the first
    # verdict; reusing the id with a changed payload is still a conflict.
    _, v = st_frame(0, "sec-p0")
    check(v["status"] == "accepted" and v["retransmit"],
          "identical receipt replayed across stations")
    code, _ = st_frame(0, "sec-p0", {"v": 2})
    check(code == 409, "receipt reused with changed payload at other station -> 409")

    # A snapshot from a different link cannot fork a station here.
    _, foreign_link = request("POST", base + "/api/links", {"name": "foreign"})
    _, foreign_snap = request(
        "POST", f"{base}/api/links/{foreign_link['id']}/snapshots", {})
    code, body = request("POST", f"{base}/api/links/{sec_id}/stations",
                         {"snapshot_id": foreign_snap["id"]})
    check(code == 409 and "foreign origin" in body["error"],
          "foreign-origin snapshot cannot fork a station")

    # A station that belongs elsewhere must not touch this link's window.
    _, foreign_station = request(
        "POST", f"{base}/api/links/{foreign_link['id']}/stations",
        {"snapshot_id": foreign_snap["id"], "name": "rogue"})
    code, _ = request(
        "POST", f"{base}/api/links/{sec_id}/stations/{foreign_station['id']}/converge", {})
    check(code == 409, "foreign station converge refused")
    code, _ = request(
        "POST", f"{base}/api/links/{sec_id}/stations/{foreign_station['id']}/frames",
        {"counter": 9, "receipt_id": "sec-rogue-frame", "payload": {}})
    check(code == 409, "foreign station frame refused")

    # One-shot convergence merges onto the higher highest and aligns both.
    code, conv = request(
        "POST", f"{base}/api/links/{sec_id}/stations/{station_id}/converge", {})
    check(code == 200 and conv["highest"] == MOD + 3,
          f"converged highest is 2^32+3 (got {conv['highest']})")
    check(conv["bitmap"] == 0b111111,
          "merged bitmap covers 2^32-2 .. 2^32+3 continuously")
    check(conv["added_primary"] == [MOD + 3, MOD + 1],
          "secondary fills frames 3 and 1 into the primary")
    check(conv["added_secondary"] == [MOD + 2, MOD],
          "primary fills frames 2 and 0 into the secondary")

    _, p_state = request("GET", f"{base}/api/links/{sec_id}")
    _, s_state = request("GET", f"{base}/api/stations/{station_id}")
    check((p_state["highest"], p_state["bitmap"])
          == (s_state["highest"], s_state["bitmap"])
          == (conv["highest"], conv["bitmap"]),
          "refreshed primary and secondary expose the identical window")
    check(p_state["recent"] == s_state["recent"],
          "both stations report the same recent positions")

    # Old frames cannot be re-accepted at either station after convergence.
    _, v = sec_frame(MOD - 100 & (MOD - 1), "sec-late-p")
    check(v["status"] == "expired", "old epoch-0 frame expired at primary after converge")
    _, v = st_frame(MOD - 100 & (MOD - 1), "sec-late-s")
    check(v["status"] == "expired", "old epoch-0 frame expired at secondary after converge")

    # Converging again changes nothing (already aligned).
    _, conv2 = request(
        "POST", f"{base}/api/links/{sec_id}/stations/{station_id}/converge", {})
    check(conv2["added_primary"] == [] and conv2["added_secondary"] == []
          and conv2["bitmap"] == conv["bitmap"],
          "a second convergence is idempotent")

    # --- input validation ---
    code, _ = request("POST", frames_url,
                      {"counter": MOD, "receipt_id": "bad", "payload": {}})
    check(code == 400, "counter >= 2^32 rejected with 400")
    code, _ = request("POST", f"{base}/api/links/deadbeef/frames",
                      {"counter": 1, "receipt_id": "x", "payload": {}})
    check(code == 404, "unknown link -> 404")

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://web:8080"
    main(base.rstrip("/"))
