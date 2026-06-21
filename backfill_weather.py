#!/usr/bin/env python3
"""
backfill_weather.py — fill empty temp_f / wind_mph / weather in rounds_summary.csv.

For any round missing weather, look up a pin coordinate for that round (from
holes.csv) and fetch historical conditions from Open-Meteo (via weather.py, stdlib,
no key) by the round's date. Writes rounds_summary.csv back in place, preserving all
columns. Graceful: a failed lookup leaves the row unchanged. Pure stdlib.

    python backfill_weather.py [store=.]
"""
from __future__ import annotations

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import weather  # noqa: E402  (lives next to this script)

MID_HOUR_UTC = 18   # ~early afternoon in US Central (course local time)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    store = sys.argv[1] if len(sys.argv) > 1 else "."
    rs_path = os.path.join(store, "rounds_summary.csv")
    holes_path = os.path.join(store, "holes.csv")
    if not os.path.exists(rs_path):
        print("backfill_weather: no rounds_summary.csv")
        return 0

    with open(rs_path, newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        fields = rdr.fieldnames
        rounds = list(rdr)

    # a representative pin per round (first hole that has coords)
    pin = {}
    if os.path.exists(holes_path):
        with open(holes_path, newline="", encoding="utf-8") as f:
            for h in csv.DictReader(f):
                rid = h.get("round_id")
                if rid in pin:
                    continue
                lat, lng = _f(h.get("pin_lat")), _f(h.get("pin_lng"))
                if lat is not None and lng is not None:
                    pin[rid] = (lat, lng)

    filled = 0
    for r in rounds:
        has = (r.get("temp_f") or "").strip() and (r.get("wind_mph") or "").strip() \
            and (r.get("weather") or "").strip()
        if has:
            continue
        rid, dt = r.get("round_id"), r.get("date")
        coord = pin.get(rid)
        if not coord or not dt:
            continue
        w = weather.fetch_round_weather(coord[0], coord[1], dt, MID_HOUR_UTC,
                                        cache_dir=store)
        if not w:
            continue
        for col in ("temp_f", "wind_mph", "wind_dir_deg", "wind_dir", "weather"):
            if col in (fields or []) and w.get(col) is not None and not (r.get(col) or "").strip():
                r[col] = w[col]
        filled += 1
        print(f"  filled {rid} ({dt}): {w.get('temp_f')}F, {w.get('wind_mph')}mph, {w.get('weather')}")

    if filled:
        tmp = rs_path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            wtr = csv.DictWriter(f, fieldnames=fields)
            wtr.writeheader()
            wtr.writerows(rounds)
        os.replace(tmp, rs_path)
        print(f"backfill_weather: filled {filled} round(s)")
    else:
        print("backfill_weather: nothing to fill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
