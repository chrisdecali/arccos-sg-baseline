#!/usr/bin/env python3
"""
validate.py — data-health check for the arccos-sg-baseline store.

Flags (as WARNINGS, never fatal — the build must not crash on data issues):
  * missing GPS on a round (no shot coordinates / no pin coords)
  * schema / column drift (a CSV missing expected columns)
  * out-of-range values (impossible distances, scores, temps, wind)
  * stale data (newest round older than ~STALE_DAYS days)

Pure stdlib. Prints warnings to stdout; exits 0 (so the pipeline continues) unless
a file is completely unreadable. Run:  python validate.py [store=.]
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import date

STALE_DAYS = 10

# expected columns per file (a subset that downstream code relies on)
SCHEMA = {
    "rounds_summary.csv": ["round_id", "date", "course", "score", "score_to_par",
                           "sg_total_arccos", "weather", "temp_f", "wind_mph"],
    "holes.csv": ["round_id", "hole_id", "par", "pin_lat", "pin_lng"],
    "shots.csv": ["round_id", "hole_id", "club", "shot_distance_yd",
                  "start_lat", "start_lng"],
    "ghin_scores.csv": ["played_at", "holes", "differential"],
    "bag.csv": ["club", "target_carry"],
}


def _read(store, name):
    path = os.path.join(store, name)
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    store = sys.argv[1] if len(sys.argv) > 1 else "."
    warns = []

    def warn(msg):
        warns.append(msg)

    rounds = _read(store, "rounds_summary.csv")
    holes = _read(store, "holes.csv")
    shots = _read(store, "shots.csv")

    # --- schema / column drift ---
    for name, cols in SCHEMA.items():
        rows = _read(store, name)
        if rows is None:
            warn(f"schema: {name} is missing")
            continue
        if rows:
            have = set(rows[0].keys())
            missing = [c for c in cols if c not in have]
            if missing:
                warn(f"schema: {name} missing columns {missing}")

    # --- missing GPS per round ---
    if shots is not None and rounds is not None:
        shot_gps = {}
        for s in shots:
            rid = s.get("round_id")
            if _f(s.get("start_lat")) is not None:
                shot_gps[rid] = shot_gps.get(rid, 0) + 1
        for r in rounds:
            rid = r.get("round_id")
            if shot_gps.get(rid, 0) == 0:
                warn(f"gps: round {rid} ({r.get('date')}) has no shot GPS "
                     f"(pull with GOLF_INCLUDE_GPS=1)")
    if holes is not None:
        no_pin = {h.get("round_id") for h in holes
                  if _f(h.get("pin_lat")) is None} & \
                 {h.get("round_id") for h in holes}
        for rid in sorted(x for x in no_pin if x):
            has_pin = any(_f(h.get("pin_lat")) is not None
                          for h in holes if h.get("round_id") == rid)
            if not has_pin:
                warn(f"gps: round {rid} has no pin coordinates")

    # --- out-of-range values ---
    for s in (shots or []):
        d = _f(s.get("shot_distance_yd"))
        if d is not None and (d < 0 or d > 400):
            warn(f"range: shot_distance_yd={d} on round {s.get('round_id')} "
                 f"hole {s.get('hole_id')} (expect 0-400)")
    for r in (rounds or []):
        tp = _f(r.get("score_to_par"))
        if tp is not None and (tp < -20 or tp > 80):
            warn(f"range: score_to_par={tp} on round {r.get('round_id')}")
        t = _f(r.get("temp_f"))
        if t is not None and (t < -20 or t > 130):
            warn(f"range: temp_f={t} on round {r.get('round_id')}")
        w = _f(r.get("wind_mph"))
        if w is not None and (w < 0 or w > 80):
            warn(f"range: wind_mph={w} on round {r.get('round_id')}")

    # --- empty weather (affects wet-aware carry) ---
    for r in (rounds or []):
        if not (r.get("weather") or "").strip():
            warn(f"weather: round {r.get('round_id')} ({r.get('date')}) has no "
                 f"weather (run backfill_weather.py)")

    # --- stale data ---
    dates = sorted(d for d in ((r.get("date") or "") for r in (rounds or [])) if d)
    if dates:
        try:
            y, m, dd = (int(x) for x in dates[-1].split("-")[:3])
            age = (date.today() - date(y, m, dd)).days
            if age > STALE_DAYS:
                warn(f"stale: newest round {dates[-1]} is {age} days old "
                     f"(> {STALE_DAYS}) — sync may be overdue")
        except (ValueError, TypeError):
            warn(f"stale: could not parse newest round date '{dates[-1]}'")
    else:
        warn("stale: no round dates found")

    # --- report ---
    if warns:
        print(f"validate.py: {len(warns)} warning(s):")
        for w in warns:
            print(f"  ! {w}")
    else:
        print("validate.py: all checks passed")
    return 0   # warnings never fail the build


if __name__ == "__main__":
    sys.exit(main())
