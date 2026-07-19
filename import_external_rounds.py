#!/usr/bin/env python3
"""import_external_rounds.py — merge non-Arccos on-course rounds into the canonical
rounds_summary.csv / holes.csv / shots.csv that gen_tracker reads.

    python import_external_rounds.py <store_dir> [--sources garmin,shotscope,manual] [--dry-run]

Phase-1 pullers (pull_garmin.py, pull_shotscope.py) drop per-source snapshot CSVs
(garmin_rounds.csv / garmin_holes.csv / garmin_shots.csv, and shotscope_*) into the store,
but nothing reads them. This maps each source's native columns onto the canonical schema,
namespaces round_id as "<source>:<native_id>", dedups against Arccos rounds by
(date, normalized-course) with **Arccos authoritative**, and APPENDS the survivors into
the canonical files.

Ordering matters. Run AFTER `pull_arccos --build` (which fully overwrites the canonical
files with Arccos-only rows) and BEFORE gen_tracker. Because the per-source snapshots are
full-history and Arccos overwrites the canonical files each cycle, the merge is idempotent:
every run re-derives the same merged output; running twice on the same store is a no-op the
second time (the appended rows are already present, so their (date, course) keys dedup).

Design notes:
  * Non-Arccos rounds have no Arccos/Broadie strokes-gained -> those columns stay BLANK
    (empty string), never 0. gen_tracker excludes blank-SG rounds from SG means and the
    SG-by-round chart, but still counts them in round totals / score stats.
  * Shot Scope's native per-shot strokes-gained lands in `sg_shot_approx` (shot-level,
    already an "approx" column); it does NOT populate the round-level `sg_*_arccos` columns,
    which would mislabel it as Arccos SG.
  * Never fatal: a missing or malformed source file logs a warning and is skipped, exactly
    like import_launch_monitor.py.

Pure stdlib.
"""
from __future__ import annotations

import csv
import datetime
import io
import os
import re
import sys
from pathlib import Path

# Precedence AFTER Arccos (which is already on disk and always wins). Among external
# sources, the first to claim a (date, course) key wins.
SOURCES_ORDER = ["shotscope", "garmin", "manual"]

# Fallback canonical column order, used only if a canonical file does not exist yet
# (cold store, no Arccos pull). When the file exists we adopt ITS header as the target
# schema, so we automatically track pull_arccos.py + public_cols (GPS filtering).
CANON_ROUND_COLS = [
    "round_id", "source", "date", "course", "tee_name", "tee_yards", "slope", "rating",
    "holes", "score", "par", "score_to_par", "pace_of_play",
    "putts", "one_putts", "three_putts", "putts_per_gir",
    "gir_hits", "gir_pct", "fairway_hits", "fairway_chances", "fairway_pct",
    "scramble_chances", "scramble_saves", "scramble_pct",
    "sand_chances_native", "sand_saves_native", "penalties",
    "avg_drive_yd", "longest_drive_yd", "avg_approach_proximity_yd",
    "sg_total_arccos", "sg_off_tee_arccos", "sg_approach_arccos",
    "sg_short_arccos", "sg_putting_arccos",
    "sg_total_broadie", "sg_off_tee_broadie", "sg_approach_broadie",
    "sg_short_broadie", "sg_putting_broadie",
    "user_hcp", "drive_hcp", "approach_hcp", "chip_hcp", "sand_hcp", "putt_hcp",
    "temp_f", "wind_mph", "wind_dir_deg", "wind_dir", "weather",
]
CANON_HOLE_COLS = [
    "round_id", "source", "date", "course", "hole_id", "par", "par_source", "shots",
    "net_score", "score_to_par", "putts", "penalties", "gir", "fairway_hit",
    "fw_miss_left", "fw_miss_right", "updown_chance_native", "updown_native",
    "sand_chance_native", "sand_save_native", "hole_len_yd", "drive_yd",
    "approach_proximity_yd", "scramble_chance", "scramble_save", "sg_hole_broadie",
]
CANON_SHOT_COLS = [
    "round_id", "source", "date", "hole_id", "shot_num", "club", "club_category",
    "shot_distance_yd", "start_dist_to_pin_yd", "end_dist_to_pin_yd",
    "is_half_swing", "lie_approx", "is_tee", "is_putt", "penalties",
    "category_approx", "sg_shot_approx",
]

# Trailing generic tokens stripped when normalizing a course name for dedup.
COURSE_SUFFIX_TOKENS = {
    "gc", "cc", "club", "course", "golf", "links", "national", "country",
    "resort", "the",
}


def _norm_course(name) -> str:
    """Normalize a course name to a dedup key: lowercase, punctuation->space, collapse
    whitespace, strip trailing generic tokens (gc / golf club / country club / ...).
    'WindRose GC' and 'WindRose Golf Club' both -> 'windrose'."""
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    toks = s.split()
    while toks and toks[-1] in COURSE_SUFFIX_TOKENS:
        toks.pop()
    if toks and toks[0] == "the":
        toks = toks[1:]
    # All-generic names ("The Links", "The Country Club") strip to nothing — falling
    # through to "" would collide every such course (and blank-course rows) into ONE
    # dedup key and silently drop real rounds. Fall back to the full lowered name.
    return " ".join(toks) or s


def _plausible_date(d) -> bool:
    """A real on-course round is dated between 2000-01-01 and ~today. Reject unparseable,
    ancient, or future dates — bad source data must not enter the canonical files (a
    future-dated round would also skew every recency-weighted stat and can zero out the
    Monte-Carlo weights, crashing the render)."""
    try:
        dt = datetime.date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return False
    # +2 days of grace for timezone edges around "today".
    return datetime.date(2000, 1, 1) <= dt <= datetime.date.today() + datetime.timedelta(days=2)


def _read(path: Path) -> list[dict]:
    """Read a CSV to a list of dicts. [] if missing/unreadable (never fatal)."""
    if not path.exists():
        return []
    try:
        raw = path.read_bytes().decode("utf-8-sig", errors="replace")
        return list(csv.DictReader(io.StringIO(raw)))
    except (OSError, csv.Error) as e:
        sys.stderr.write(f"warn: could not read {path.name}: {e}\n")
        return []


def _header(path: Path) -> list[str] | None:
    """The canonical column order = the existing file's header, or None if absent."""
    if not path.exists():
        return None
    try:
        raw = path.read_bytes().decode("utf-8-sig", errors="replace")
        r = csv.reader(io.StringIO(raw))
        for row in r:
            return row
    except (OSError, csv.Error):
        return None
    return None


def _ensure_source_col(path: Path) -> list[str] | None:
    """Adopt the existing file's header; if it predates the `source` column, atomically
    rewrite the file ONCE with `source` inserted after round_id and legacy rows tagged
    'arccos' (safe: only pull_arccos ever wrote the canonical files pre-`source`).

    Appending source-tagged rows under an old header would either silently drop the tag
    (extrasaction='ignore') or write rows wider than the header — so heal the schema
    instead. Returns the (possibly upgraded) header, or None if the file is absent."""
    header = _header(path)
    if header is None or "source" in header:
        return header
    idx = header.index("round_id") + 1 if "round_id" in header else 0
    new_header = header[:idx] + ["source"] + header[idx:]
    rows = _read(path)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=new_header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r["source"] = r.get("source") or "arccos"
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in new_header})
    os.replace(tmp, path)
    sys.stderr.write(f"note: upgraded {path.name} schema (+source column, legacy rows tagged arccos)\n")
    return new_header


def _sub(a, b):
    """a - b as an int when both parse, else '' (for score_to_par)."""
    try:
        return int(float(a)) - int(float(b))
    except (TypeError, ValueError):
        return ""


def _fairway_hit(outcome) -> str:
    """Garmin fairwayShotOutcome -> canonical fairway_hit (1 on a clear hit, else blank).
    We do NOT infer a miss side from unknown enum values — better blank than wrong."""
    s = str(outcome or "").strip().upper()
    if s in ("HIT", "FAIRWAY", "FAIRWAY_HIT", "IN_FAIRWAY"):
        return "1"
    return ""


# ------------------------------------------------------------------ per-source mappers
# Each mapper returns a dict keyed by CANONICAL column names. Absent canonical columns are
# filled with "" at write time. round_id is namespaced "<source>:<native>".

def _garmin_round(r: dict) -> dict:
    return {
        "round_id": f"garmin:{r.get('round_id', '')}",
        "source": "garmin",
        "date": r.get("date", ""),
        "course": r.get("course", ""),
        "holes": r.get("holes", ""),
        "score": r.get("score", ""),
        "putts": r.get("putts", ""),
        "penalties": r.get("penalties", ""),
        "user_hcp": r.get("playerHandicap", ""),
    }


def _garmin_hole(h: dict, date: str, course: str) -> dict:
    par, strokes = h.get("par", ""), h.get("strokes", "")
    return {
        "round_id": f"garmin:{h.get('round_id', '')}",
        "source": "garmin", "date": date, "course": course,
        "hole_id": h.get("hole", ""),
        "par": par, "shots": strokes, "net_score": strokes,
        "score_to_par": _sub(strokes, par),
        "putts": h.get("putts", ""), "penalties": h.get("penalties", ""),
        "fairway_hit": _fairway_hit(h.get("fairwayShotOutcome")),
    }


def _garmin_shot(s: dict, date: str) -> dict:
    return {
        "round_id": f"garmin:{s.get('round_id', '')}",
        "source": "garmin", "date": date,
        "hole_id": s.get("hole", ""), "shot_num": s.get("shotOrder", ""),
        # Garmin gives a raw clubId, not a club name -> leave `club` BLANK so it can't
        # pollute the club-distance/bag analysis (which keys on real club names). Mapping
        # clubId -> name needs a Garmin club dictionary; that's a later pass.
        "shot_distance_yd": s.get("distance_yds", ""),
        "lie_approx": s.get("start_lie", ""),
    }


def _shotscope_round(r: dict) -> dict:
    return {
        "round_id": f"shotscope:{r.get('round_id', '')}",
        "source": "shotscope",
        "date": r.get("date", ""),
        "course": r.get("course", ""),
        "score": r.get("score", ""),
        "score_to_par": r.get("score_to_par", ""),
        "user_hcp": r.get("handicap", ""),
    }


def _shotscope_hole(h: dict, date: str, course: str) -> dict:
    par, score = h.get("par", ""), h.get("score", "")
    return {
        "round_id": f"shotscope:{h.get('round_id', '')}",
        "source": "shotscope", "date": date, "course": course,
        "hole_id": h.get("holeNumber", ""),
        "par": par, "shots": score, "net_score": score,
        "score_to_par": _sub(score, par),
        "putts": h.get("putts", ""), "penalties": h.get("penalties", ""),
        "sand_save_native": h.get("sandSave", ""),
    }


def _shotscope_shot(s: dict, date: str) -> dict:
    return {
        "round_id": f"shotscope:{s.get('round_id', '')}",
        "source": "shotscope", "date": date,
        "hole_id": s.get("holeNumber", ""), "shot_num": s.get("shotNumber", ""),
        "club": s.get("clubName", ""), "lie_approx": s.get("lie", ""),
        "start_dist_to_pin_yd": s.get("distanceToPin", ""),
        "sg_shot_approx": s.get("strokesGained", ""),  # native shot-level SG (approx column)
    }


def _manual_round(r: dict) -> dict:
    # Manual entry is authored in canonical column names already; passthrough + namespace.
    out = dict(r)
    out["round_id"] = f"manual:{r.get('round_id', '')}"
    out["source"] = "manual"
    return out


def _manual_hole(h: dict, date: str, course: str) -> dict:
    out = dict(h)
    out["round_id"] = f"manual:{h.get('round_id', '')}"
    out["source"] = "manual"
    out.setdefault("date", date)
    out.setdefault("course", course)
    return out


def _manual_shot(s: dict, date: str) -> dict:
    out = dict(s)
    out["round_id"] = f"manual:{s.get('round_id', '')}"
    out["source"] = "manual"
    out.setdefault("date", date)
    return out


MAPPERS = {
    "garmin": (_garmin_round, _garmin_hole, _garmin_shot),
    "shotscope": (_shotscope_round, _shotscope_hole, _shotscope_shot),
    "manual": (_manual_round, _manual_hole, _manual_shot),
}


def _backfill_from_holes(rounds: list[dict], holes: list[dict]) -> None:
    """Fill round-level par + holes count from the round's holes when the source summary
    omits them: Garmin has no total par; Shot Scope has neither total par nor a holes count.
    A holes count matters because downstream (statcard course standings) drops rounds with
    holes < 9, so a Shot Scope round with no holes count would silently vanish from the
    clubhouse."""
    par_by_round: dict[str, int] = {}
    par_n: dict[str, int] = {}
    n_by_round: dict[str, int] = {}
    for h in holes:
        rid = h.get("round_id")
        if rid is None:
            continue
        n_by_round[rid] = n_by_round.get(rid, 0) + 1
        p = h.get("par")
        if p in (None, ""):
            continue
        try:
            par_by_round[rid] = par_by_round.get(rid, 0) + int(float(p))
            par_n[rid] = par_n.get(rid, 0) + 1
        except (TypeError, ValueError):
            continue
    for r in rounds:
        rid = r.get("round_id")
        # Only trust the summed par when EVERY hole contributed one — a partial sum
        # (8 of 9 pars known) understates par and inflates score_to_par.
        if (not r.get("par") and rid in par_by_round
                and par_n.get(rid) == n_by_round.get(rid)):
            r["par"] = par_by_round[rid]
            if not r.get("score_to_par"):
                r["score_to_par"] = _sub(r.get("score"), r["par"])
        if not r.get("holes") and rid in n_by_round:
            r["holes"] = n_by_round[rid]


def _append(path: Path, cols: list[str], rows: list[dict]) -> None:
    """Append rows to a canonical CSV (create with header if absent). extrasaction='ignore'
    drops any mapped key not in the canonical header (e.g. GPS columns stripped by
    public_cols)."""
    if not rows:
        return
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})


def merge_store(store, sources=None, dry_run=False) -> dict:
    """Merge external per-source round snapshots into the canonical files. Returns a summary
    dict. Callable directly (tests) or via main()."""
    store = Path(store)
    sources = sources or SOURCES_ORDER
    round_cols = _ensure_source_col(store / "rounds_summary.csv") or CANON_ROUND_COLS
    hole_cols = _ensure_source_col(store / "holes.csv") or CANON_HOLE_COLS
    shot_cols = _ensure_source_col(store / "shots.csv") or CANON_SHOT_COLS

    existing = _read(store / "rounds_summary.csv")
    seen: set[tuple[str, str]] = {
        (r.get("date") or "", _norm_course(r.get("course"))) for r in existing
    }
    # Same-day audit map (finding: cross-source course-name divergence can evade dedup —
    # e.g. "TPC Sawgrass" vs "Stadium Course at TPC Sawgrass" normalize differently, so
    # the same physical round would merge as two). We keep such rounds but WARN, so a
    # double-count is auditable instead of silent.
    dates_seen: dict[str, set[str]] = {}
    for r in existing:
        dates_seen.setdefault(r.get("date") or "", set()).add(_norm_course(r.get("course")))

    add_rounds: list[dict] = []
    add_holes: list[dict] = []
    add_shots: list[dict] = []
    dropped: list[str] = []
    warnings: list[str] = []
    per_source: dict[str, int] = {}

    for src in SOURCES_ORDER:
        if src not in sources:
            continue
        rr = _read(store / f"{src}_rounds.csv")
        if not rr:
            continue
        rmap, hmap, smap = MAPPERS[src]
        meta = {str(r.get("round_id")): (r.get("date", ""), r.get("course", "")) for r in rr}
        kept_ids: set[str] = set()
        for r in rr:
            date, course = r.get("date") or "", r.get("course") or ""
            if not date or not course:
                dropped.append(f"{src}:{r.get('round_id')} (missing date/course)")
                continue
            if not _plausible_date(date):
                dropped.append(f"{src}:{r.get('round_id')} ({date}) — implausible date, skipped")
                continue
            key = (date, _norm_course(course))
            if key in seen:
                dropped.append(f"{src}:{r.get('round_id')} ({date} {course}) — dup of existing round")
                continue
            if dates_seen.get(date):
                # Same DAY as an existing round but a different course key: legit second
                # course, or the same physical round under a diverged course name
                # ("TPC Sawgrass" vs "Stadium Course at TPC Sawgrass"). Keep it, but flag
                # for audit — a silent double-count is the one unrecoverable outcome.
                warnings.append(
                    f"{src}:{r.get('round_id')} ({date} {course}) — same-day round with a "
                    f"different course name than {sorted(dates_seen[date])}; audit for a "
                    f"cross-source name mismatch (possible double-count)")
            seen.add(key)
            dates_seen.setdefault(date, set()).add(key[1])
            kept_ids.add(str(r.get("round_id")))
            add_rounds.append(rmap(r))
        if not kept_ids:
            per_source[src] = 0
            continue
        src_holes = []
        for h in _read(store / f"{src}_holes.csv"):
            if str(h.get("round_id")) in kept_ids:
                d, c = meta.get(str(h.get("round_id")), ("", ""))
                src_holes.append(hmap(h, d, c))
        for s in _read(store / f"{src}_shots.csv"):
            if str(s.get("round_id")) in kept_ids:
                d, _c = meta.get(str(s.get("round_id")), ("", ""))
                add_shots.append(smap(s, d))
        _backfill_from_holes([r for r in add_rounds if r.get("source") == src], src_holes)
        add_holes.extend(src_holes)
        per_source[src] = len(kept_ids)

    summary = {
        "added_rounds": len(add_rounds),
        "added_holes": len(add_holes),
        "added_shots": len(add_shots),
        "per_source": per_source,
        "dropped": dropped,
        "warnings": warnings,
    }
    if dry_run:
        return summary

    _append(store / "rounds_summary.csv", round_cols, add_rounds)
    _append(store / "holes.csv", hole_cols, add_holes)
    _append(store / "shots.csv", shot_cols, add_shots)
    return summary


def main() -> None:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    sources = None
    if "--sources" in args:
        sources = [s.strip() for s in args[args.index("--sources") + 1].split(",") if s.strip()]
    pos = [a for a in args if not a.startswith("--")
           and (sources is None or a != ",".join(sources))]
    if not pos:
        sys.exit("usage: import_external_rounds.py <store_dir> [--sources garmin,shotscope,manual] [--dry-run]")
    summary = merge_store(pos[0], sources=sources, dry_run=dry)
    tag = "would add" if dry else "added"
    print(f"{tag}: rounds={summary['added_rounds']} holes={summary['added_holes']} "
          f"shots={summary['added_shots']} per_source={summary['per_source']}")
    for d in summary["dropped"]:
        print(f"  dedup/skip: {d}")
    for w in summary["warnings"]:
        print(f"  WARN: {w}")


if __name__ == "__main__":
    main()
