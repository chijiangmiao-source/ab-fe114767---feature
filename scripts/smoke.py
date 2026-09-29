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

    # --- secondary station: same-source snapshot, outage, one-shot convergence ---
    _, prim = request("POST", base + "/api/links", {"name": "smoke-pair"})
    pid = prim["id"]

    def pframe(counter, rid, payload=None):
        return request("POST", f"{base}/api/links/{pid}/frames",
                       {"counter": counter, "receipt_id": rid,
                        "payload": payload if payload is not None else {"v": 1}})

    pframe(MOD - 2, "b-a")
    pframe(MOD - 1, "b-b")
    code, sec = request("POST", f"{base}/api/links/{pid}/stations",
                        {"name": "smoke-backup"})
    check(code == 201 and sec["role"] == "secondary",
          "secondary station created from primary snapshot")
    sid = sec["id"]
    check(sec["highest"] == MOD - 1 and sec["snapshot"]["highest"] == MOD - 1,
          "secondary calibrates extended seq on the common basis")

    def sframe(counter, rid, payload=None):
        return request("POST", f"{base}/api/links/{sid}/frames",
                       {"counter": counter, "receipt_id": rid,
                        "payload": payload if payload is not None else {"v": 1}})

    # Disconnect window: distinct out-of-order frames, across the wrap.
    pframe(1, "b-p1")                        # primary -> MOD+1
    sframe(0, "b-s1")                        # secondary -> MOD
    sframe(2, "b-s2")                        # secondary -> MOD+2
    # Same receipt first adjudicated at the secondary replays at the primary.
    _, v = pframe(0, "b-s1")
    check(v["retransmit"] and v["status"] == "accepted",
          "identical receipt retransmitted at the other station replays verdict")
    # Reusing the id with changed payload/link is still refused.
    code, _ = sframe(0, "b-s1", {"v": 9})
    check(code == 409, "receipt id reuse with changed payload at secondary -> 409")

    code, conv = request(
        "POST", f"{base}/api/links/{pid}/stations/{sid}/converge")
    check(code == 200 and conv["highest"] == MOD + 2,
          "convergence projects both bitmaps onto the higher highest")
    check(conv["recent"] == [MOD + 2, MOD + 1, MOD, MOD - 1, MOD - 2],
          f"merged window is the union around the wrap (got {conv['recent']})")
    check(MOD in conv["added"], "convergence reports positions filled by secondary")

    _, p_after = request("GET", f"{base}/api/links/{pid}")
    _, s_after = request("GET", f"{base}/api/links/{sid}")
    check((p_after["highest"], p_after["bitmap"])
          == (s_after["highest"], s_after["bitmap"]),
          "both stations expose the identical merged window after refresh")
    check(s_after["converged"], "secondary snapshot marked converged")

    # Convergence is one-shot; foreign/closed/non-secondary attempts are refused.
    code, _ = request("POST", f"{base}/api/links/{pid}/stations/{sid}/converge")
    check(code == 409, "converging the same snapshot twice is refused")
    code, _ = sframe(99, "b-late")
    check(code == 409, "new frame at a converged secondary is refused")
    # A secondary from another source cannot move this primary's window.
    _, other_p = request("POST", base + "/api/links", {"name": "smoke-foreign"})
    code, _ = request(
        "POST", f"{base}/api/links/{other_p['id']}/stations/{sid}/converge")
    check(code == 409, "foreign secondary cannot converge into another primary")
    # Old frames never re-enter: merged highest is MOD+2, counter 0 stays known.
    _, v = pframe(0, "b-dupcheck")
    check(v["status"] == "duplicate", "frame already covered stays duplicate")

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
