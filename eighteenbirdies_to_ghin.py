#!/usr/bin/env python3
"""
18Birdies -> GHIN helper.

Turns your 18Birdies account-data export into a **GHIN posting worksheet** (+ CSV)
so you can get 18Birdies-only rounds onto your USGA handicap.

READ-ONLY BY DESIGN: this tool NEVER posts to GHIN and NEVER handles a password.
It parses a local 18Birdies export file and (optionally, with a pasted GHIN read
token) dedupes against your existing GHIN scores and fills in course rating/slope.
You then post the handful of missing rounds in the GHIN app yourself — which is
the simplest and safest path because:
  * Arccos already auto-posts to GHIN, so only rounds logged in 18Birdies but NOT
    Arccos need posting — usually few.
  * Posting writes to your real Handicap Index; a wrong rating/tee corrupts it.
  * No reverse-engineered write path to break or violate GHIN's ToS.

INPUT — your own data, no login:
  18Birdies -> https://18birdies.com/download-account-data/ -> 18Birdies_archive.json

USAGE:
  python3 eighteenbirdies_to_ghin.py 18Birdies_archive.json
      -> 18birdies_rounds.csv  +  GHIN_ENTRY_WORKSHEET.md

  # Optional: dedupe vs GHIN + auto-fill rating/slope (READ-ONLY GET calls).
  # Paste a GHIN Bearer: GHIN.com -> DevTools -> Network -> any api2.ghin.com XHR
  # -> copy the Authorization "Bearer ..." value (lives ~12h). Never your password.
  python3 eighteenbirdies_to_ghin.py 18Birdies_archive.json \
      --ghin-bearer "eyJ..." --ghin-id 1234567 --default-tee "Blue" --home-course "WindRose"

Posting itself stays manual (GHIN app). See docs/18birdies-to-ghin.md for the
optional, approval-gated auto-post path (intentionally NOT enabled here).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

GHIN_BASE = "https://api2.ghin.com/api/v1"
GHIN_DELAY_S = 0.6  # polite spacing between GHIN read calls
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148 Safari/537.36"


# ---------------------------------------------------------------------------
# 18Birdies export parsing (schema from the official account-data export;
# verified against ericlu28/18birdies-wrapped + mfannin099/18_birdies_project)
# ---------------------------------------------------------------------------

def load_archive(path: str) -> dict:
    if not os.path.exists(path):
        sys.exit(f"Error: {path} not found. Download it from "
                 "https://18birdies.com/download-account-data/")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"Error: {path} is not valid JSON: {e}")


def _course_name_map(data: dict) -> dict[str, str]:
    """course uuid -> course name, from clubData.playedClubs[]."""
    out: dict[str, str] = {}
    club = (data.get("myData") or data).get("clubData") or data.get("clubData") or {}
    for c in (club.get("playedClubs") or []):
        cid = (c.get("id") or {})
        cid = cid.get("id") if isinstance(cid, dict) else c.get("id")
        name = c.get("name") or c.get("clubName")
        if cid and name:
            out[str(cid)] = name
    return out


def extract_rounds(data: dict) -> list[dict]:
    """Normalize 18Birdies rounds. Tolerant of myData wrapper presence/absence."""
    root = data.get("myData") or data
    activity = root.get("activityData") or {}
    rounds = activity.get("rounds") or root.get("rounds") or []
    names = _course_name_map(data)

    out = []
    for r in rounds:
        cid = r.get("clubId") or {}
        cid = cid.get("id") if isinstance(cid, dict) else cid
        ts = r.get("timestamp")
        date = ""
        if ts:
            try:
                date = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, OverflowError, OSError):
                date = ""
        holes = r.get("holeStrokes") or []
        out.append({
            "round_id": r.get("id"),
            "date": date,
            "course": names.get(str(cid), f"course:{cid}"),
            "course_uuid": cid,
            "holes_played": len([h for h in holes if h]) or r.get("holeCount") or (18 if holes else None),
            "gross": r.get("strokes"),
            "to_par": r.get("score"),
            "hole_scores": " ".join(str(h) for h in holes) if holes else "",
        })
    # newest first
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Optional GHIN reads (dedupe + course/tee rating lookup). READ-ONLY GET.
# ---------------------------------------------------------------------------

def _ghin_get(path: str, bearer: str, params: Optional[dict] = None) -> Optional[Any]:
    q = dict(params or {})
    q.setdefault("source", "GHINcom")
    url = f"{GHIN_BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", bearer if bearer.lower().startswith("bearer") else f"Bearer {bearer}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    try:
        time.sleep(GHIN_DELAY_S)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  GHIN GET {path} -> HTTP {e.code} (token expired? it lasts ~12h)", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  GHIN GET {path} -> {type(e).__name__}: {e}", file=sys.stderr)
    return None


def ghin_existing_scores(bearer: str, ghin_id: str) -> list[dict]:
    d = _ghin_get(f"/golfers/{ghin_id}/scores.json", bearer)
    scores = (d or {}).get("scores") if isinstance(d, dict) else (d if isinstance(d, list) else [])
    norm = []
    for s in scores or []:
        norm.append({
            "date": (s.get("played_at") or "")[:10],
            "course": s.get("course_name") or "",
            "gross": s.get("adjusted_gross_score"),
        })
    return norm


def ghin_resolve_course(bearer: str, name: str, tee: Optional[str]) -> dict:
    """Best-effort course_id + tee rating/slope from a course name. Read-only."""
    res = _ghin_get("/courses/search.json", bearer,
                    {"name": name, "country": "USA", "include_tee_sets": "true"})
    courses = (res or {}).get("courses") if isinstance(res, dict) else (res if isinstance(res, list) else [])
    if not courses:
        return {}
    course = courses[0]  # best match; verify in worksheet
    cid = course.get("CourseID") or course.get("course_id") or course.get("id")
    info = {"ghin_course": course.get("FullName") or course.get("course_name") or course.get("name"),
            "ghin_course_id": cid}
    if not (cid and tee):
        return info
    tr = _ghin_get(f"/courses/{cid}/tee_set_ratings.json", bearer,
                   {"gender": "M", "number_of_holes": "18", "tee_set_status": "Active"})
    tees = (tr or {}).get("TeeSets") or (tr or {}).get("tee_sets") or []
    for t in tees:
        tname = (t.get("TeeSetRatingName") or t.get("name") or "")
        if tee.lower() in tname.lower():
            ratings = t.get("Ratings") or []
            total = next((x for x in ratings if (x.get("RatingType") or "").lower() == "total"), None) or (ratings[0] if ratings else {})
            info.update({"tee": tname,
                         "tee_set_id": t.get("TeeSetRatingId") or t.get("tee_set_id"),
                         "course_rating": total.get("CourseRating") or t.get("CourseRating"),
                         "slope_rating": total.get("SlopeRating") or t.get("SlopeRating")})
            break
    return info


def is_in_ghin(rnd: dict, existing: list[dict]) -> bool:
    for e in existing:
        if e["date"] != rnd["date"]:
            continue
        g1, g2 = rnd.get("gross"), e.get("gross")
        if g1 and g2 and abs(int(g1) - int(g2)) <= 2:  # raw vs adjusted gross differ slightly
            return True
    return False


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

CSV_COLS = ["date", "course", "holes_played", "gross", "to_par", "score_type_suggest",
            "tee", "course_rating", "slope_rating", "already_in_ghin",
            "ghin_course_id", "tee_set_id", "hole_scores", "round_id", "course_uuid"]


def write_outputs(rounds: list[dict], out_dir: str, deduped: bool) -> tuple[int, int]:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "18birdies_rounds.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rounds:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CSV_COLS})

    to_post = [r for r in rounds if not r.get("already_in_ghin")]
    lines = [
        "# GHIN entry worksheet (from 18Birdies export)",
        "",
        "Post these rounds in the **GHIN app** (or GHIN.com): *Post Score* -> pick the",
        "course + tee shown -> enter the score (hole-by-hole if you want GHIN to do the",
        "Net-Double-Bogey adjustment). This tool does NOT post — it just lists what to enter.",
        "",
        f"- 18Birdies rounds found: **{len(rounds)}**",
    ]
    if deduped:
        lines.append(f"- Already on GHIN (skip): **{len(rounds) - len(to_post)}**")
        lines.append(f"- **To post (not in GHIN): {len(to_post)}**")
    else:
        lines.append("- (Run with `--ghin-bearer`/`--ghin-id` to auto-skip rounds already on GHIN.)")
    lines += ["",
              "score_type: **H** if your home course, **A** otherwise. Never **T** unless it was a real tournament.",
              "Blank tee/rating/slope = pick them from the course in the GHIN app (or rerun with --ghin-bearer + --default-tee).",
              "",
              "| Post? | Date | Course | Holes | Gross | Tee | Rating | Slope | Type | Hole scores |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rounds:
        post = "" if r.get("already_in_ghin") else "✅"
        lines.append("| {p} | {d} | {c} | {h} | {g} | {t} | {cr} | {sl} | {st} | {hs} |".format(
            p=post, d=r["date"], c=r["course"], h=r.get("holes_played") or "",
            g=r.get("gross") or "", t=r.get("tee") or "", cr=r.get("course_rating") or "",
            sl=r.get("slope_rating") or "", st=r.get("score_type_suggest") or "",
            hs=r.get("hole_scores") or ""))
    lines += ["",
              "> Source: unofficial — 18Birdies account-data export + GHIN read-only lookups.",
              "> Posting writes to your official USGA index; double-check rating/slope/tee and",
              "> don't re-post rounds Arccos already sent. GPS/PII excluded."]
    with open(os.path.join(out_dir, "GHIN_ENTRY_WORKSHEET.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(rounds), len(to_post)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archive", help="Path to 18Birdies_archive.json")
    p.add_argument("--out-dir", default=".", help="Where to write outputs (default: cwd)")
    p.add_argument("--ghin-bearer", help="GHIN Bearer JWT (read-only; dedupe + rating lookup)")
    p.add_argument("--ghin-id", help="Your GHIN number (for dedupe)")
    p.add_argument("--default-tee", help="Tee you usually play (e.g. Blue) for rating/slope lookup")
    p.add_argument("--home-course", help="Substring of your home course -> score_type H")
    args = p.parse_args()

    rounds = extract_rounds(load_archive(args.archive))
    if not rounds:
        sys.exit("No rounds found in the export. Is this an 18Birdies account-data file?")

    for r in rounds:  # suggest score_type
        r["score_type_suggest"] = "H" if (args.home_course and args.home_course.lower()
                                          in (r["course"] or "").lower()) else "A"

    deduped = False
    if args.ghin_bearer and args.ghin_id:
        print("Reading GHIN scores for dedupe...", file=sys.stderr)
        existing = ghin_existing_scores(args.ghin_bearer, args.ghin_id)
        deduped = bool(existing)
        course_cache: dict[str, dict] = {}
        for r in rounds:
            r["already_in_ghin"] = is_in_ghin(r, existing)
            if r["already_in_ghin"]:
                continue
            key = (r["course"] or "").lower()
            if key not in course_cache:
                course_cache[key] = ghin_resolve_course(args.ghin_bearer, r["course"], args.default_tee)
            r.update({k: v for k, v in course_cache[key].items() if v is not None})

    total, to_post = write_outputs(rounds, args.out_dir, deduped)
    print(f"Wrote {args.out_dir}/18birdies_rounds.csv and GHIN_ENTRY_WORKSHEET.md")
    print(f"  rounds: {total}" + (f" | to post (not in GHIN): {to_post}" if deduped else ""))
    print("This tool did NOT post anything. Enter the worksheet rounds in the GHIN app.")


if __name__ == "__main__":
    main()
