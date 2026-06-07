#!/usr/bin/env python3
"""
pull_18birdies.py — parse your 18Birdies account-data export into a clean
`18birdies_rounds.csv` for the repo (raw 18Birdies-native rounds + stats).

READ-ONLY, no login, no password: works on the official export file you download
yourself (https://18birdies.com/download-account-data/ -> 18Birdies_archive.json).
18Birdies has no usable API; this is the supported way to get your data.

This is the 18Birdies-native record (score + scoring distribution + fairways/GIR/
putts that 18Birdies tracked). It is separate from the GHIN scores (which only hold
date/course/gross/rating). Note: 18Birdies has NO shot/GPS data and its strokes-
gained is premium-gated (sentinel) — so SG is intentionally omitted.

Usage:
    python3 pull_18birdies.py /path/to/18Birdies_archive.json
        -> writes 18birdies_rounds.csv into the repo (./arccos_out or this dir)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "arccos_out") if os.path.isdir(os.path.join(HERE, "arccos_out")) else HERE

COLS = ["date", "course", "holes", "gross", "to_par",
        "fairways_hit", "fairway_chances", "fairway_pct",
        "gir", "gir_chances", "gir_pct", "putts",
        "eagles", "birdies", "pars", "bogeys", "double_plus", "aces",
        "hole_scores", "round_id"]


def _g(d: Any, *names, default=None):
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d.get(n)
    return default


def _pct(num, den):
    try:
        return round(100 * num / den, 1) if den else None
    except (TypeError, ZeroDivisionError):
        return None


def course_names(data: dict) -> dict[str, str]:
    root = data.get("myData") or data
    club = root.get("clubData") or data.get("clubData") or {}
    out = {}
    for c in (club.get("playedClubs") or []):
        cid = c.get("id")
        cid = cid.get("id") if isinstance(cid, dict) else cid
        if cid and c.get("name"):
            out[str(cid)] = c["name"]
    return out


def extract(data: dict) -> list[dict]:
    root = data.get("myData") or data
    rounds = (root.get("activityData") or {}).get("rounds") or root.get("rounds") or []
    names = course_names(data)
    out = []
    for r in rounds:
        cid = r.get("clubId")
        cid = cid.get("id") if isinstance(cid, dict) else cid
        ts = r.get("timestamp")
        date = ""
        if ts:
            try:
                date = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, OverflowError, OSError):
                pass
        st = r.get("stats") or {}
        holes = r.get("holeStrokes") or []
        fw_hit = _g(st, "fairwayMiddles")
        fw_chances = _g(st, "fairwayHoleCount")
        gir = _g(st, "gir")
        gir_chances = _g(st, "girHoleCount")
        out.append({
            "date": date,
            "course": names.get(str(cid), f"course:{cid}"),
            "holes": len([h for h in holes if h]) or _g(r, "holeCount", default=(18 if holes else None)),
            "gross": _g(r, "strokes"),
            "to_par": _g(r, "score"),
            "fairways_hit": fw_hit,
            "fairway_chances": fw_chances,
            "fairway_pct": _pct(fw_hit, fw_chances),
            "gir": gir,
            "gir_chances": gir_chances,
            "gir_pct": _pct(gir, gir_chances),
            "putts": _g(st, "putts"),
            "eagles": _g(st, "eagles"),
            "birdies": _g(st, "birdies"),
            "pars": _g(st, "pars"),
            "bogeys": _g(st, "bogeys"),
            "double_plus": _g(st, "doubleBogeyOrWorse"),
            "aces": _g(st, "aces"),
            "hole_scores": " ".join(str(h) for h in holes) if holes else "",
            "round_id": r.get("id"),
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archive", help="Path to 18Birdies_archive.json")
    p.add_argument("--out-dir", default=OUT_DIR)
    args = p.parse_args()

    if not os.path.exists(args.archive):
        sys.exit(f"Error: {args.archive} not found. Download it from "
                 "https://18birdies.com/download-account-data/")
    try:
        with open(args.archive, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"Error: {args.archive} is not valid JSON: {e}")

    rounds = extract(data)
    if not rounds:
        sys.exit("No rounds found — is this an 18Birdies account-data export?")
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, "18birdies_rounds.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in rounds:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLS})
    print(f"Wrote {path} — {len(rounds)} rounds")


if __name__ == "__main__":
    main()
