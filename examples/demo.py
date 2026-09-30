#!/usr/bin/env python3
"""End-to-end calling example for the split timing adjudication API.

Only uses the Python standard library. By default it talks to the
docker-compose service:

    python3 examples/demo.py
    BASE_URL=http://127.0.0.1:8000 python3 examples/demo.py

The script walks through every requirement:
  1. create a race (start/cutoff, ordered points + finish, closed duration
     intervals per segment, chip bindings)
  2. rejected configuration: invalid time window, bad interval, dup chip
  3. submit scans out of order: idempotent replay, conflicting replay,
     unknown chip / unknown point rejection
  4. provisional results: earliest valid finish, lex-min scan-no tiebreak,
     did-not-finish cases
  4b. per-point signed integer-second clock offsets: query (unset == zero),
      set/replace, non-integer and unknown-point rejection, provisional
      results recomputed from calibrated times while raw times are kept
  4c. pre-seal offset preview: current vs proposed adjudication per runner
      (status, raw/calibrated used scans, finish, rank and deltas) from one
      consistent state; empty/duplicate/unknown/non-integer rejected; the
      preview stores nothing
  5. revoke a scan before sealing -> provisional result recomputed
  6. seal the race -> frozen snapshot; afterwards submit/revoke, offset
     changes and previews are rejected, but the snapshot keeps the
     pre-seal version
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# Fixed UTC instant so the demo is reproducible.
T0 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)
CUTOFF = T0 + timedelta(hours=1)

# Matches an ISO-8601 instant ending in "Z" so response timestamps compare
# cleanly against the "+00:00" strings produced by at() for request bodies.
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _normalize(value):
    if isinstance(value, str) and _ISO_Z.match(value):
        return value[:-1] + "+00:00"
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def at(minute: int, second: int = 0) -> str:
    return (T0 + timedelta(minutes=minute, seconds=second)).isoformat()


def request(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read()
            parsed = json.loads(payload) if payload else None
            return resp.status, _normalize(parsed)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        parsed = json.loads(payload) if payload else None
        return exc.code, _normalize(parsed)


def step(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(status: int, body) -> None:
    print(f"HTTP {status}")
    print(json.dumps(body, indent=2, ensure_ascii=False, default=str))


def expect(status: int, actual: int, label: str) -> None:
    if status != actual:
        print(f"!! expected {status} for {label}, got {actual}")
        sys.exit(1)
    print(f"ok [{actual}] {label}")


def main() -> None:
    # ------------------------------------------------------------------ #
    step("1. Create the race")
    # ------------------------------------------------------------------ #
    race_body = {
        "name": "City 10K 2026",
        "start_time": at(0),
        "cutoff_time": at(60),
        # Ordered points; the last one is the finish (FIN).
        "points": [
            {"code": "C1", "name": "Checkpoint 1"},
            {"code": "C2", "name": "Checkpoint 2"},
            {"code": "FIN", "name": "Finish", "is_finish": True},
        ],
        # start->C1, C1->C2, C2->FIN: closed non-negative intervals (sec)
        "segments": [
            {"min_seconds": 300, "max_seconds": 1800},
            {"min_seconds": 300, "max_seconds": 1800},
            {"min_seconds": 300, "max_seconds": 1800},
        ],
        "runners": [
            {"bib": "A01", "name": "Alice", "chip": "CHIP-A"},
            {"bib": "B02", "name": "Bob", "chip": "CHIP-B"},
            {"bib": "C03", "name": "Carol", "chip": "CHIP-C"},
            {"bib": "D04", "name": "Dan", "chip": "CHIP-D"},
        ],
    }
    status, body = request("POST", "/races", race_body)
    show(status, body)
    expect(201, status, "race created")
    race_id = body["id"]
    rp = f"/races/{race_id}"
    runner_id = {r["bib"]: r["id"] for r in body["runners"]}

    # ------------------------------------------------------------------ #
    step("2. Invalid configurations are rejected (422)")
    # ------------------------------------------------------------------ #
    bad = dict(race_body)
    bad["name"] = "bad window"
    bad["cutoff_time"] = at(-1)
    status, body = request("POST", "/races", bad)
    show(status, body)
    expect(422, status, "cutoff before start rejected")

    bad = json.loads(json.dumps(race_body))
    bad["name"] = "bad interval"
    bad["segments"][0] = {"min_seconds": 900, "max_seconds": 300}
    status, body = request("POST", "/races", bad)
    expect(422, status, "min > max interval rejected")

    bad = json.loads(json.dumps(race_body))
    bad["name"] = "dup chip"
    bad["runners"][1]["chip"] = "CHIP-A"
    status, body = request("POST", "/races", bad)
    expect(422, status, "duplicate chip rejected")

    bad = json.loads(json.dumps(race_body))
    bad["name"] = "segment count mismatch"
    bad["segments"].pop()
    status, body = request("POST", "/races", bad)
    expect(422, status, "segment/point count mismatch rejected")

    # ------------------------------------------------------------------ #
    step("3. Submit scans out of order")
    # ------------------------------------------------------------------ #
    def scan(scan_no, chip, point, minute, second=0):
        return request(
            "POST",
            f"{rp}/scans",
            {
                "scan_no": scan_no,
                "chip": chip,
                "point": point,
                "occurred_at": at(minute, second),
            },
        )

    # Alice: clean 10/20/30 path, plus an out-of-order early FIN scan that
    # creates a slower alternative path (25 -> 30 only 5 minutes < 300s
    # minimum, so it is unusable as C2->FIN, leaving the canonical path).
    status, _ = scan("A-030", "CHIP-A", "FIN", 30)
    expect(201, status, "Alice FIN (submitted first, out of order)")
    status, _ = scan("A-010", "CHIP-A", "C1", 10)
    expect(201, status, "Alice C1")
    status, _ = scan("A-020", "CHIP-A", "C2", 20)
    expect(201, status, "Alice C2")

    # Bob: never crosses the finish -> DNF.
    status, _ = scan("B-010", "CHIP-B", "C1", 11)
    expect(201, status, "Bob C1")
    status, _ = scan("B-020", "CHIP-B", "C2", 22)
    expect(201, status, "Bob C2 (no FIN scan)")

    # Carol: C2->FIN gap too short (2 minutes) -> no valid path -> DNF.
    status, _ = scan("C-010", "CHIP-C", "C1", 10)
    expect(201, status, "Carol C1")
    status, _ = scan("C-020", "CHIP-C", "C2", 20)
    expect(201, status, "Carol C2")
    status, _ = scan("C-022", "CHIP-C", "FIN", 22)
    expect(201, status, "Carol FIN too early (still accepted as a scan)")

    # Dan: two complete paths with identical finish time (30 min).
    #   path X: "DA01","DB01","DC01"
    #   path Y: "DA02","DB02","DC02"
    # Both finish at the same instant, so the lex-min tuple wins:
    # ("DA01","DB01","DC01") < ("DA02","DB02","DC02").
    status, _ = scan("DA02", "CHIP-D", "C1", 10)
    expect(201, status, "Dan C1 scan DA02")
    status, _ = scan("DA01", "CHIP-D", "C1", 10)
    expect(201, status, "Dan C1 scan DA01")
    status, _ = scan("DB02", "CHIP-D", "C2", 20)
    expect(201, status, "Dan C2 scan DB02")
    status, _ = scan("DB01", "CHIP-D", "C2", 20)
    expect(201, status, "Dan C2 scan DB01")
    status, _ = scan("DC02", "CHIP-D", "FIN", 30)
    expect(201, status, "Dan FIN scan DC02")
    status, _ = scan("DC01", "CHIP-D", "FIN", 30)
    expect(201, status, "Dan FIN scan DC01")

    # --- idempotency & conflict --------------------------------------- #
    status, body = scan("A-010", "CHIP-A", "C1", 10)
    show(status, body)
    expect(200, status, "same scan_no + same content is idempotent replay")
    assert body["replayed"] is True

    status, body = scan("A-010", "CHIP-A", "C1", 11)
    show(status, body)
    expect(409, status, "same scan_no + different content conflicts")

    status, body = scan("X-001", "CHIP-UNKNOWN", "C1", 10)
    show(status, body)
    expect(404, status, "unknown chip rejected")

    status, body = scan("X-002", "CHIP-A", "NEVER", 10)
    show(status, body)
    expect(404, status, "unknown point rejected")

    # --- batch endpoint ------------------------------------------------- #
    status, body = request(
        "POST",
        f"{rp}/scans/batch",
        {
            "scans": [
                # unknown chip
                {"scan_no": "Z1", "chip": "NOPE", "point": "C1",
                 "occurred_at": at(10)},
                # valid new
                {"scan_no": "BATCH-1", "chip": "CHIP-A", "point": "C1",
                 "occurred_at": at(12)},
                # idempotent replay
                {"scan_no": "A-010", "chip": "CHIP-A", "point": "C1",
                 "occurred_at": at(10)},
            ]
        },
    )
    show(status, body)
    expect(200, status, "batch accepted with per-item statuses")
    assert [item["status_code"] for item in body] == [404, 201, 200]

    # BATCH-1 gives Alice a C1 at minute 12; earliest finish still uses
    # A-010(10) -> A-020(20) -> A-030(30).

    # ------------------------------------------------------------------ #
    step("4. Provisional results (before sealing)")
    # ------------------------------------------------------------------ #
    status, results = request("GET", f"{rp}/results")
    show(status, results)
    expect(200, status, "provisional results")
    by_bib = {r["bib"]: r for r in results["results"]}

    assert by_bib["A01"]["status"] == "finished"
    assert [u["scan_no"] for u in by_bib["A01"]["used_scans"]] == [
        "A-010", "A-020", "A-030"
    ]
    assert by_bib["A01"]["rank"] == 1
    print("ok Alice fastest, rank 1, scans A-010/A-020/A-030")

    assert by_bib["B02"]["status"] == "did_not_finish"
    print("ok Bob DNF (no finish scan)")

    assert by_bib["C03"]["status"] == "did_not_finish"
    print("ok Carol DNF (segment outside allowed interval)")

    # ------------------------------------------------------------------ #
    step("4b. Point clock offsets: query, calibrate, recompute, reject")
    # ------------------------------------------------------------------ #
    # Fresh race: every point reports offset zero (unset == zero).
    status, body = request("GET", f"{rp}/points/offsets")
    show(status, body)
    expect(200, status, "list all point offsets")
    assert [(o["point"], o["offset_seconds"]) for o in body["offsets"]] == [
        ("C1", 0), ("C2", 0), ("FIN", 0),
    ]
    assert body["offsets_version"] == 0
    v0 = body["offsets_version"]

    status, body = request("GET", f"{rp}/points/C2/offset")
    expect(200, status, "get one point offset (unset -> zero)")
    assert body["offset_seconds"] == 0 and body["updated_at"] is None

    status, body = request("GET", f"{rp}/points/NOPE/offset")
    show(status, body)
    expect(404, status, "unknown point offset query rejected")

    # C2's clock is 60 s slow: raw 20:00 calibrates to 19:00. Carol's
    # FIN (22:00) is then exactly 3 minutes away: C1(10) -> C2(19) = 9 min
    # is inside [5,30], and C2(19) -> FIN(22) = 3 min is still below the
    # 5-minute lower bound, so she stays DNF.
    status, body = request(
        "PUT", f"{rp}/points/C2/offset", {"offset_seconds": -60}
    )
    show(status, body)
    expect(201, status, "first set of C2 offset (new -> 201)")
    assert body["offset_seconds"] == -60 and body["replaced"] is False

    status, results = request("GET", f"{rp}/results")
    assert results["offsets_version"] == v0 + 1
    by_bib = {r["bib"]: r for r in results["results"]}
    assert by_bib["C03"]["status"] == "did_not_finish"
    print("ok C2 offset -60 alone does not make Carol's path valid")

    # FIN clock 120 s fast: raw 22:00 calibrates to 24:00, so C2(19) ->
    # FIN(24) is exactly 300 s -> the closed-interval lower boundary; the
    # finish calibrates to 24:00, still inside the 60-minute cutoff.
    status, body = request(
        "PUT", f"{rp}/points/FIN/offset", {"offset_seconds": 120}
    )
    expect(201, status, "first set of FIN offset")

    status, body = request("GET", f"{rp}/points/FIN/offset")
    expect(200, status, "get one point offset (set value)")
    assert body["offset_seconds"] == 120 and body["updated_at"] is not None

    status, results = request("GET", f"{rp}/results")
    show(status, results)
    assert results["offsets_version"] == v0 + 2
    by_bib = {r["bib"]: r for r in results["results"]}
    carol = by_bib["C03"]
    assert carol["status"] == "finished"
    assert carol["finish_time"] == at(24), carol["finish_time"]
    assert carol["total_seconds"] == 24 * 60
    used = {u["point"]: u for u in carol["used_scans"]}
    # Raw scan timestamps are kept; calibrated timestamps are shown too.
    assert used["C2"]["occurred_at"] == at(20)
    assert used["C2"]["calibrated_at"] == at(19)
    assert used["FIN"]["occurred_at"] == at(22)
    assert used["FIN"]["calibrated_at"] == at(24)
    assert used["C2"]["segment_seconds"] == 9 * 60
    assert used["FIN"]["segment_seconds"] == 5 * 60
    print("ok Carol finishes after C2/FIN calibration (raw kept, calibrated shown)")

    # C2 offset shifts Alice's and Dan's C2 scans to 19:00 while their FIN
    # scans (FIN clock 120 s fast) calibrate to 32:00; Carol calibrates to
    # 24:00: the ranking is Carol, then Alice, then Dan (Alice vs Dan tie at
    # 32:00 breaks on scan-no sequence).
    assert by_bib["A01"]["finish_time"] == at(32)
    assert by_bib["D04"]["finish_time"] == at(32)
    assert carol["rank"] == 1
    assert by_bib["A01"]["rank"] == 2
    assert by_bib["D04"]["rank"] == 3
    print("ok ranking recomputed from calibrated finish times")

    # Non-integer offsets are explicitly rejected, including whole floats.
    status, body = request(
        "PUT", f"{rp}/points/C1/offset", {"offset_seconds": 0.5}
    )
    show(status, body)
    expect(422, status, "fractional offset rejected")
    status, body = request(
        "PUT", f"{rp}/points/C1/offset", {"offset_seconds": 1.0}
    )
    expect(422, status, "whole-valued float (1.0) rejected as non-integer")
    status, body = request(
        "PUT", f"{rp}/points/NOPE/offset", {"offset_seconds": 5}
    )
    show(status, body)
    expect(404, status, "unknown point offset set rejected")

    # Replacing C2's offset is HTTP 200 with replaced=true. Reset C2/FIN to
    # zero so the remainder of the demo sees the original adjudication.
    # ------------------------------------------------------------------ #
    step("4c. Offset preview: current vs proposed, computed but not stored")
    # ------------------------------------------------------------------ #
    # Before resetting anything for real, staff preview the effect of
    # resetting C2/FIN to zero: the "current" side is adjudicated with the
    # stored offsets (C2=-60, FIN=120), the "proposed" side with the request
    # values layered on top; points not listed keep their current value.
    status, preview = request("POST", f"{rp}/offsets/preview", {"offsets": [
        {"point": "C2", "offset_seconds": 0},
        {"point": "FIN", "offset_seconds": 0},
    ]})
    show(status, preview)
    expect(200, status, "offset preview")
    assert preview["offsets_version"] == v0 + 2
    assert [(o["point"], o["offset_seconds"])
            for o in preview["current_offsets"]] == [
        ("C1", 0), ("C2", -60), ("FIN", 120),
    ]
    assert [(o["point"], o["offset_seconds"])
            for o in preview["proposed_offsets"]] == [
        ("C1", 0), ("C2", 0), ("FIN", 0),
    ]
    pv = {r["bib"]: r for r in preview["results"]}

    # Carol drops back to DNF under the proposal; finish/rank are null.
    carol_pv = pv["C03"]
    assert carol_pv["current"]["status"] == "finished"
    assert carol_pv["current"]["rank"] == 1
    assert carol_pv["proposed"]["status"] == "did_not_finish"
    assert carol_pv["proposed"]["finish_time"] is None
    assert carol_pv["proposed"]["rank"] is None
    assert carol_pv["proposed"]["used_scans"] == []
    assert carol_pv["status_changed"] is True
    assert carol_pv["rank_change"] is None
    print("ok preview: Carol finished@1 -> DNF (empty finish time and rank)")

    # Alice moves back to rank 1 (rank_change +1, finish 120 s earlier).
    alice_pv = pv["A01"]
    assert alice_pv["current"]["rank"] == 2
    assert alice_pv["proposed"]["rank"] == 1
    assert alice_pv["status_changed"] is False
    assert alice_pv["rank_change"] == 1
    assert alice_pv["finish_time_change_seconds"] == -120.0
    # Both sides carry the raw and calibrated times of the used scans.
    assert alice_pv["current"]["used_scans"][-1]["calibrated_at"] == at(32)
    assert alice_pv["proposed"]["used_scans"][-1]["calibrated_at"] == at(30)
    assert alice_pv["proposed"]["used_scans"][-1]["occurred_at"] == at(30)
    print("ok preview: Alice rank 2->1, finish_time_change -120 s, raw kept")

    # The preview must not persist anything: offsets and version unchanged.
    status, body = request("GET", f"{rp}/points/offsets")
    assert body["offsets_version"] == v0 + 2
    assert [(o["point"], o["offset_seconds"]) for o in body["offsets"]] == [
        ("C1", 0), ("C2", -60), ("FIN", 120),
    ]
    print("ok preview wrote no offsets and did not bump offsets_version")

    # Preview-specific rejections: empty list, duplicate point, unknown
    # point, and non-integer offsets reuse the same strict validation.
    status, body = request("POST", f"{rp}/offsets/preview", {"offsets": []})
    expect(422, status, "empty preview list rejected")
    status, body = request("POST", f"{rp}/offsets/preview", {"offsets": [
        {"point": "C2", "offset_seconds": -60},
        {"point": "C2", "offset_seconds": 30},
    ]})
    expect(422, status, "duplicate preview point rejected")
    status, body = request("POST", f"{rp}/offsets/preview", {"offsets": [
        {"point": "NOPE", "offset_seconds": 5},
    ]})
    expect(404, status, "unknown preview point rejected")
    status, body = request("POST", f"{rp}/offsets/preview", {"offsets": [
        {"point": "C2", "offset_seconds": 5.0},
    ]})
    expect(422, status, "non-integer preview offset rejected")

    status, body = request(
        "PUT", f"{rp}/points/C2/offset", {"offset_seconds": 0}
    )
    show(status, body)
    expect(200, status, "replacing an existing offset -> 200 replaced=true")
    assert body["replaced"] is True
    request("PUT", f"{rp}/points/FIN/offset", {"offset_seconds": 0})
    status, results = request("GET", f"{rp}/results")
    by_bib = {r["bib"]: r for r in results["results"]}
    assert by_bib["C03"]["status"] == "did_not_finish"
    assert results["offsets_version"] == v0 + 4
    print("ok reset offsets -> provisional results recompute to original")

    assert by_bib["D04"]["status"] == "finished"
    assert [u["scan_no"] for u in by_bib["D04"]["used_scans"]] == [
        "DA01", "DB01", "DC01"
    ]
    assert by_bib["D04"]["finish_time"] == by_bib["A01"]["finish_time"]
    assert by_bib["D04"]["rank"] == 2
    print("ok Dan tied finish time -> lex-min scan sequence DA01/DB01/DC01")

    # single-runner view
    status, body = request(
        "GET", f"{rp}/runners/{runner_id['A01']}/result"
    )
    expect(200, status, "single runner result")
    assert body["bib"] == "A01"

    # ------------------------------------------------------------------ #
    step("5. Revoke a scan before sealing -> results recompute")
    # ------------------------------------------------------------------ #
    # Revoke Alice's FIN scan. She drops to DNF provisionally.
    status, body = request("DELETE", f"{rp}/scans/A-030")
    show(status, body)
    expect(200, status, "scan revoked")

    status, results = request("GET", f"{rp}/results")
    by_bib = {r["bib"]: r for r in results["results"]}
    assert by_bib["A01"]["status"] == "did_not_finish"
    print("ok Alice provisionally DNF after her FIN scan was revoked")

    # double revoke rejected
    status, body = request("DELETE", f"{rp}/scans/A-030")
    show(status, body)
    expect(409, status, "revoking an already-revoked scan rejected")

    # Revoking an unknown scan 404s
    status, body = request("DELETE", f"{rp}/scans/NO-SUCH-SCAN")
    expect(404, status, "unknown scan revocation 404")

    # ------------------------------------------------------------------ #
    step("6. Seal the race and verify the frozen snapshot")
    # ------------------------------------------------------------------ #
    status, sealed = request("POST", f"{rp}/seal")
    show(status, sealed)
    expect(200, status, "race sealed")
    assert sealed["sealed"] is True

    # Sealing is idempotent and returns the same snapshot.
    status, sealed_again = request("POST", f"{rp}/seal")
    expect(200, status, "re-seal is idempotent")
    assert sealed_again["results"] == sealed["results"]
    print("ok repeated seal returns the same frozen snapshot")

    # GET results now comes from the snapshot (Alice stays DNF).
    status, body = request("GET", f"{rp}/results")
    by_bib = {r["bib"]: r for r in body["results"]}
    assert by_bib["A01"]["status"] == "did_not_finish"
    print("ok snapshot serves results after sealing")

    # After sealing: submit and revoke are rejected.
    status, body = scan("LATE", "CHIP-A", "FIN", 35)
    show(status, body)
    expect(409, status, "scan submission after sealing rejected")

    status, body = request("DELETE", f"{rp}/scans/BATCH-1")
    show(status, body)
    expect(409, status, "revocation after sealing rejected")

    # Offset changes are rejected after sealing; the snapshot (computed at
    # the pre-seal offset version, zero) is unaffected.
    status, body = request(
        "PUT", f"{rp}/points/C1/offset", {"offset_seconds": 30}
    )
    show(status, body)
    expect(409, status, "offset change after sealing rejected")

    # Previews are likewise rejected for a sealed race.
    status, body = request(
        "POST", f"{rp}/offsets/preview",
        {"offsets": [{"point": "C1", "offset_seconds": 30}]},
    )
    show(status, body)
    expect(409, status, "offset preview after sealing rejected")

    # Offset queries still work and keep reporting the frozen version.
    status, body = request("GET", f"{rp}/points/offsets")
    expect(200, status, "offsets still queryable after sealing")
    assert body["sealed"] is True
    assert all(o["offset_seconds"] == 0 for o in body["offsets"])

    status, body = request("GET", f"{rp}/results")
    assert body["offsets_version"] == v0 + 4
    print("ok sealed snapshot keeps the pre-seal offset version")

    print("\nALL DEMO STEPS PASSED")


if __name__ == "__main__":
    main()
