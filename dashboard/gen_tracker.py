#!/usr/bin/env python3
"""
gen_tracker.py — single-page strokes-gained dashboard for arccos-sg-baseline.

Reads the data store (CSVs + JSON written by pull_arccos.py / pull_ghin.py) and
renders one self-contained HTML page: game plan, KPIs, WHS index projection,
aim-by-club, dispersion explorer, approach/scrambling/putting, measured-vs-target
bag, an all-rounds satellite map, SG-by-round, posted scores, trouble holes, and
cost-of-misses.

Design rules (see CLAUDE.md — these are load-bearing):
  * Read CSVs by COLUMN NAME, never position. Tolerate missing columns/files.
  * Distances use the BEST-THIRD strike, not the average (mishits poison the mean).
  * Dispersion cleaning: carry floor = 0.8 x median carry, then lateral IQR (1.5x).
  * The bag stays STRICTLY DESCENDING — never suggest a yardage that puts a longer
    club below a shorter one; hold the target instead.
  * Label measured vs modeled everywhere.
  * SG levers overlap ~35-40% — don't additively stack; apply a 0.62 efficiency
    factor to combined totals.
  * PURE STDLIB — no third-party deps. Charts are inline SVG; the interactive map uses
    a CDN (Leaflet+Esri) that loads client-side only when the HTML opens in a browser.

Usage:
    python dashboard/gen_tracker.py [store_dir=.] [out=docs/index.html]

compute(store_dir) is pure (files in -> dict out) so a trend module can call it.
"""
from __future__ import annotations

import csv
import html
import json
import math
import os
import random
import shutil
import statistics
import sys

PLAYER_NAME = os.environ.get("GOLF_PLAYER_NAME", "Chris Cole")
# Optional per-player context — set for the owner's own build, left empty for others so
# nobody else's dashboard inherits this player's event/home course.
GOLF_EVENT = os.environ.get("GOLF_EVENT", "").strip()
GOLF_HOME_COURSE = os.environ.get("GOLF_HOME_COURSE", "").strip()
import zlib
from datetime import date

# Pure stdlib — charts are hand-rolled inline SVG (no matplotlib), so the only output
# is one self-contained HTML file.
YARD_PER_M = 1.0936132983


# --------------------------------------------------------------------------- IO
def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def _copy_font_assets(outdir: str) -> None:
    """Copy self-hosted dashboard fonts next to the rendered HTML."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
    dst = os.path.join(outdir, "assets", "fonts")
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _f(x):
    """float or None (blank/garbage tolerant)."""
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in ("nan", "none", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _i(x):
    v = _f(x)
    return int(v) if v is not None else None


def _truthy(x) -> bool:
    return str(x).strip() in ("1", "1.0", "true", "True", "yes")


def _is_right_handed(x) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        return x.strip().lower() not in ("f", "false")
    return True


def _opposite_side(side: str) -> str:
    return "right" if side == "left" else "left"


def _side_abbr(side: str) -> str:
    return "L" if side == "left" else "R"


def _swing_fault(side: str, right_handed: bool) -> dict:
    hook_side = "left" if right_handed else "right"
    if side == hook_side:
        return {
            "fault": "hook",
            "miss": "hook/pull",
            "fix": "the face is closing relative to your swing path at impact",
            "face": "closing",
            "face_past": "closed",
            "shape": "draw",
        }
    return {
        "fault": "slice",
        "miss": "push/slice",
        "fix": "the face is open relative to your swing path at impact",
        "face": "open",
        "face_past": "open",
        "shape": "fade",
    }


def _slug(course: str, date: str) -> str:
    """Stable filename slug for a round, e.g. 'WindRose_GC_2026-06-06'."""
    base = "".join(c if c.isalnum() else "_" for c in (course or "round"))
    while "__" in base:
        base = base.replace("__", "_")
    return f"{base.strip('_')}_{date or 'x'}"


def _round5(x):
    """Round to the nearest 5 yards (None-safe)."""
    return None if x is None else int(round(x / 5.0) * 5)


def _cid(name):
    """Alphanumeric id for a club, safe as an HTML class/value suffix."""
    return "".join(c for c in str(name or "") if c.isalnum()) or "x"


# ----------------------------------------------------------------------- WHS
# Number of posted scores -> (how many lowest differentials count, adjustment).
# This is the USGA WHS allotment for fewer than 20 scores. The index is the mean
# of the lowest N differentials, minus the adjustment. It rises mechanically as
# more scores post (the adjustment shrinks) — NOT a sign of getting worse.
WHS_TABLE = {
    3: (1, 2.0), 4: (1, 1.0), 5: (1, 0.0),
    6: (2, 1.0), 7: (2, 0.0), 8: (2, 0.0),
    9: (3, 0.0), 10: (3, 0.0), 11: (3, 0.0),
    12: (4, 0.0), 13: (4, 0.0), 14: (4, 0.0),
    15: (5, 0.0), 16: (5, 0.0),
    17: (6, 0.0), 18: (6, 0.0),
    19: (7, 0.0), 20: (8, 0.0),
}


def whs_index(diffs: list[float]):
    """WHS Handicap Index from a list of score differentials (any length)."""
    diffs = sorted(d for d in diffs if d is not None)
    n = len(diffs)
    if n < 3:
        return None
    count, adj = WHS_TABLE.get(min(n, 20), (8, 0.0))
    best = diffs[:count]
    return round(statistics.fmean(best) - adj, 1)


# ------------------------------------------------------------- target bag (18B)
# Carry yardages (18Birdies set). Only data-backed change so far: 5i 175 -> 170.
# Kept STRICTLY DESCENDING. Used as the modeled "target" to compare measured carry.
TARGET_BAG = [
    ("Driver", 250), ("3 Wood", 225), ("5 Wood", 200), ("Hybrid", 185),
    ("5 Iron", 170), ("6 Iron", 165), ("7 Iron", 155), ("8 Iron", 145),
    ("9 Iron", 135), ("Pitching Wedge", 120), ("50 Wedge", 105),
    ("54 Wedge", 90), ("58 Wedge", 80),
]
# Modeled total->carry haircut by category. CONDITION-AWARE: firm/dry ground rolls
# out (DRY); wet ground (rain/drizzle) barely rolls so carry ~= total (WET). The
# per-club factor is blended by how wet that club's shots actually were. Carry stays
# modeled until launch-monitor data exists (planned: Tee Box, July).
CARRY_FACTOR = {"Driver": 0.90, "Wood": 0.93, "Hybrid": 0.95, "Iron": 0.97,
                "Wedge": 0.98, "Putter": 1.0}                                 # dry/firm
WET_FACTOR = {"Driver": 0.98, "Wood": 0.98, "Hybrid": 0.98, "Iron": 0.99,
              "Wedge": 0.99, "Putter": 1.0}                                   # rain/soft
# Distance estimate = recency-weighted best-third, validated by Monte Carlo bootstrap.
RECENCY_HALF_LIFE_DAYS = 21   # a round this many days old counts half as much
MC_ITERS = 1500               # bootstrap resamples per club (deterministic per seed)
GROUP_OF = {"Driver": "Woods", "Wood": "Woods", "Hybrid": "Woods",
            "Iron": "Irons", "Wedge": "Wedges"}


# ------------------------------------------------------------------- geometry
def _enu_yards(lat, lng, lat0, lng0):
    """Local east/north offset in yards from (lat0,lng0) — equirectangular."""
    e = math.radians(lng - lng0) * math.cos(math.radians(lat0)) * 6378137.0
    n = math.radians(lat - lat0) * 6378137.0
    return e * YARD_PER_M, n * YARD_PER_M


def _lateral_offset(s, e, pin):
    """Signed perpendicular yards of END from the START->PIN line.
    Positive = right of the target line, negative = left. None if any point missing."""
    if not all(s) or not all(e) or not all(pin):
        return None
    ax, ay = 0.0, 0.0
    bx, by = _enu_yards(pin[0], pin[1], s[0], s[1])     # pin relative to start
    px, py = _enu_yards(e[0], e[1], s[0], s[1])         # end relative to start
    abx, aby = bx - ax, by - ay
    L = math.hypot(abx, aby)
    if L < 1e-6:
        return None
    # cross product (AB x AP) > 0 => AP is left of AB (CCW). Flip so right is +.
    cross = abx * (py - ay) - aby * (px - ax)
    return -cross / L


def _miss_vs(start, end, target):
    """Miss of END relative to the START->TARGET line, in yards:
    (long_short, left_right) where +long = past the target, +right = right of line.
    Reuses _lateral_offset for the (tested) right-positive lateral. None if missing."""
    lr = _lateral_offset(start, end, target)
    if lr is None:
        return None
    tx, ty = _enu_yards(target[0], target[1], start[0], start[1])
    L = math.hypot(tx, ty)
    if L < 1.0:
        return None
    ex, ey = _enu_yards(end[0], end[1], start[0], start[1])
    along = (ex * tx + ey * ty) / L          # progress toward the target
    return (along - L, lr)                    # +long (past), +right


def _clean_third(vals):
    """Average of the best (longest) third — robust 'clean strike' distance."""
    vals = sorted(v for v in vals if v is not None and v > 0)
    if not vals:
        return None
    k = max(1, round(len(vals) / 3))
    return statistics.fmean(vals[-k:])


def _date_ord(s):
    """'YYYY-MM-DD' -> day ordinal (for spacing recency weights). None if unparsable."""
    try:
        y, m, d = (int(x) for x in str(s).split("-")[:3])
        return date(y, m, d).toordinal()
    except (ValueError, TypeError):
        return None


def _recency_weight(day_ord, newest_ord):
    """Exponential decay: a shot RECENCY_HALF_LIFE_DAYS older counts half."""
    if day_ord is None or newest_ord is None:
        return 1.0
    return 0.5 ** ((newest_ord - day_ord) / RECENCY_HALF_LIFE_DAYS)


_WET_WORDS = ("rain", "drizzle", "shower", "thunder", "storm", "snow", "sleet", "wet")


def _is_wet(weather) -> bool:
    """True if the round's conditions imply soft, low-rollout ground."""
    return any(w in (weather or "").lower() for w in _WET_WORDS)


def _roll_factor(cat, frac_wet):
    """Blend the dry and wet total->carry factor by how wet the shots were."""
    dry = CARRY_FACTOR.get(cat, 0.97)
    wet = WET_FACTOR.get(cat, 0.99)
    return dry + (wet - dry) * max(0.0, min(1.0, frac_wet))


def _mc_best_third(weighted, seed, iters=MC_ITERS):
    """Monte-Carlo (recency-weighted bootstrap) of the best-third distance.

    `weighted` is a list of (distance, recency_weight). Resample n shots with
    replacement weighted by recency, take the best third, average; repeat. Returns
    (median, lo10, hi90, n) — the median is the robust estimate, lo/hi an 80%
    band so small samples read as uncertain. Deterministic given `seed`.
    """
    dists = [d for d, w in weighted if d and d > 0]
    wts = [w for d, w in weighted if d and d > 0]
    n = len(dists)
    if n == 0:
        return None
    if n == 1:
        return (dists[0], dists[0], dists[0], 1)
    rng = random.Random(seed)
    k = max(1, round(n / 3))
    means = []
    for _ in range(iters):
        samp = rng.choices(dists, weights=wts, k=n)
        samp.sort(reverse=True)
        means.append(sum(samp[:k]) / k)
    means.sort()
    return (means[iters // 2], means[int(0.10 * iters)], means[int(0.90 * iters)], n)


def _iqr_filter(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 4:
        return vals
    # inclusive method = robust on the small samples we have (n often 4–7)
    q = statistics.quantiles(vals, n=4, method="inclusive")
    lo, hi = q[0] - 1.5 * (q[2] - q[0]), q[2] + 1.5 * (q[2] - q[0])
    return [v for v in vals if lo <= v <= hi]


def _iqr_bounds(vals):
    """1.5*IQR fences for `vals` — for dropping clear outliers from 2D miss data."""
    vals = [v for v in vals if v is not None]
    if len(vals) < 4:
        return (-1e9, 1e9)
    q = statistics.quantiles(vals, n=4, method="inclusive")
    iqr = q[2] - q[0]
    return (q[0] - 1.5 * iqr, q[2] + 1.5 * iqr)


# --------------------------------------------------------------------- compute
def compute(store: str) -> dict:
    rounds = _read_csv(os.path.join(store, "rounds_summary.csv"))
    holes = _read_csv(os.path.join(store, "holes.csv"))
    shots = _read_csv(os.path.join(store, "shots.csv"))
    clubs = _read_csv(os.path.join(store, "clubs.csv"))
    ghin = _read_csv(os.path.join(store, "ghin_scores.csv"))
    bands = _read_csv(os.path.join(store, "sga_bands.csv"))
    career = _read_json(os.path.join(store, "career_stats.json"), {})
    disp = _read_json(os.path.join(store, "dispersion.json"), {})
    profile = _read_json(os.path.join(store, "player_profile.json"), {}) or {}
    fallback_profile = {}
    if not profile or "isRightStance" not in profile:
        fallback_profile = _read_json(os.path.join(store, "profile.json"), {}) or {}
        if not profile:
            profile = fallback_profile
    stance = profile.get("isRightStance")
    if stance is None:
        stance = fallback_profile.get("isRightStance")
    right_handed = _is_right_handed(stance)

    # ---- single-source-of-truth config files ----
    # club_map.json fixes Arccos mis-tags at ingest — rename each shot/club's club.
    club_map = (_read_json(os.path.join(store, "club_map.json"), {}) or {}).get("map", {})
    for row in shots + clubs:
        if row.get("club") in club_map:
            row["club"] = club_map[row["club"]]
    # bag.csv = source of truth for clubs + target carries (kept descending).
    bag_csv = _read_csv(os.path.join(store, "bag.csv"))
    if bag_csv:
        target_bag = [(b["club"], _i(b.get("target_carry")) or 0) for b in bag_csv
                      if b.get("club") and _i(b.get("target_carry"))]
        bag_specs = {b["club"]: b for b in bag_csv if b.get("club")}
    else:
        # No bag.csv → NO target bag. Never fall back to the built-in default: it is
        # one specific player's 18Birdies set, and leaking it into another player's
        # dashboard drives their gapping/club suggestions off someone else's targets.
        target_bag, bag_specs = [], {}
    # launch_monitor.csv = MEASURED carries; prefer over modeled when present.
    lm_acc: dict[str, list] = {}
    for r in _read_csv(os.path.join(store, "launch_monitor.csv")):
        if r.get("club") and _f(r.get("carry_yd")):
            lm_acc.setdefault(r["club"], []).append(_f(r["carry_yd"]))
    lm_carry = {c: statistics.fmean(v) for c, v in lm_acc.items()}
    # Optional Trackman swing metrics — dormant until you log a Tee Box session, then
    # they sharpen the swing-pattern coaching (face_to_path: - = closed/draw-hook bias,
    # + = open/fade-slice bias; club_path: + = in-to-out; attack: + = up).
    lm_metrics: dict = {}
    _macc: dict = {}
    for r in _read_csv(os.path.join(store, "launch_monitor.csv")):
        c, f2p = r.get("club"), _f(r.get("face_to_path_deg"))
        if not c or f2p is None:
            continue
        _macc.setdefault(c, []).append(
            (f2p, _f(r.get("club_path_deg")), _f(r.get("face_angle_deg")),
             _f(r.get("attack_deg"))))
    for c, vs in _macc.items():
        def _mavg(j, rows=vs):
            xs = [t[j] for t in rows if t[j] is not None]
            return round(statistics.fmean(xs), 1) if xs else None
        lm_metrics[c] = {"f2p": _mavg(0), "club_path": _mavg(1),
                         "face_angle": _mavg(2), "attack": _mavg(3), "n": len(vs)}
    _f2ps = [m["f2p"] for m in lm_metrics.values() if m["f2p"] is not None]
    lm_bag_f2p = round(statistics.fmean(_f2ps), 1) if _f2ps else None

    # ---- player / index ----
    # GHIN's official WHS Handicap Index is authoritative — use it verbatim. Our own
    # whs_index() is only a fallback (for the projection what-if, or before GHIN has
    # established an index): it can't perfectly mirror WHS edge cases like 9-hole
    # pairing or the low-HI cap, so never let it override the official number.
    ghin_profile = _read_json(os.path.join(store, "ghin_profile.json"), {})
    diffs = [_f(r.get("differential")) for r in ghin if _i(r.get("holes")) == 18]
    idx = _f(ghin_profile.get("handicap_index"))   # official GHIN value
    if idx is None:
        idx = whs_index(diffs)                      # fallback estimate
    if idx is None:
        idx = (disp.get("player") or {}).get("hcp_index")

    # ---- KPIs (prefer measured career rates) ----
    kr = career.get("key_rates", {})
    sg_arccos = career.get("strokes_gained_arccos", {})

    # ---- per-round (newest last) ----
    rounds_sorted = sorted(rounds, key=lambda r: r.get("date") or "")
    round_rows = []
    for r in rounds_sorted:
        round_rows.append({
            "round_id": r.get("round_id"), "date": r.get("date"),
            "course": r.get("course"), "tee": r.get("tee_name"),
            "slug": _slug(r.get("course"), r.get("date")),
            "yards": _i(r.get("tee_yards")), "par": _i(r.get("par")),
            "score": _i(r.get("score")), "to_par": _i(r.get("score_to_par")),
            "putts": _i(r.get("putts")), "gir": _f(r.get("gir_pct")),
            "fairway": _f(r.get("fairway_pct")), "scramble": _f(r.get("scramble_pct")),
            "sg_total": _f(r.get("sg_total_arccos")),
            "sg_off_tee": _f(r.get("sg_off_tee_arccos")),
            "sg_approach": _f(r.get("sg_approach_arccos")),
            "sg_short": _f(r.get("sg_short_arccos")),
            "sg_putting": _f(r.get("sg_putting_arccos")),
            "temp_f": _f(r.get("temp_f")), "wind_mph": _f(r.get("wind_mph")),
            "weather": r.get("weather") or r.get("conditions"),
        })

    # ---- per-club distance: recency-weighted best-third, Monte-Carlo validated ----
    # One estimate per club, used by BOTH the bag and the dispersion explorer so they
    # never disagree. Shots carry their round date (recency) and whether the round was
    # wet (rollout) so the total->carry factor reflects real conditions.
    club_meta = {c.get("club"): c for c in clubs}
    wet_by_date = {r.get("date"): _is_wet(r.get("weather") or r.get("conditions"))
                   for r in rounds}
    shots_by_club: dict[str, list[tuple]] = {}   # club -> [(distance, day_ord, is_wet)]
    for s in shots:
        if _truthy(s.get("is_putt")) or _i(s.get("penalties")):
            continue
        club = s.get("club")
        if not club or club == "Putter":
            continue
        d = _f(s.get("shot_distance_yd"))
        if d:
            shots_by_club.setdefault(club, []).append(
                (d, _date_ord(s.get("date")), wet_by_date.get(s.get("date"), False)))
    newest_ord = max((o for sh in shots_by_club.values() for _d, o, _w in sh
                      if o is not None), default=None)
    club_mc = {}      # club -> (median_total, lo, hi, n)
    club_factor = {}  # club -> condition-blended total->carry factor
    club_wet = {}     # club -> recency-weighted fraction of shots played wet
    for name, sh in shots_by_club.items():
        cat = (club_meta.get(name, {}) or {}).get("club_category") or _club_cat(name)
        ws = [_recency_weight(o, newest_ord) for _d, o, _w in sh]
        club_mc[name] = _mc_best_third(
            [(d, w) for (d, _o, _x), w in zip(sh, ws)],
            seed=zlib.crc32(name.encode()))
        wsum = sum(ws)
        frac_wet = (sum(w for (_d, _o, x), w in zip(sh, ws) if x) / wsum) if wsum else 0.0
        club_wet[name] = frac_wet
        club_factor[name] = _roll_factor(cat, frac_wet)

    bag = []
    prev_carry = None  # enforce strictly descending
    for name, target in target_bag:
        cm = club_meta.get(name, {})
        cat = cm.get("club_category") or _club_cat(name)
        mc = club_mc.get(name)
        n = mc[3] if mc else 0
        total_est = mc[0] if mc else _f(cm.get("smart_distance_yd"))
        factor = club_factor.get(name, CARRY_FACTOR.get(cat, 0.97))   # wet-aware
        # PREFER a launch-monitor (measured) carry when we have one for this club.
        lm = name in lm_carry
        if lm:
            measured = _round5(lm_carry[name])
        else:
            measured = _round5(total_est * factor) if total_est else None
        # Trust MEASURED when it's a launch-monitor number, OR enough on-course
        # samples AND within ~15% of target (else it's noise/mis-tag).
        confident = lm or (measured is not None and n >= 8
                           and abs(measured - target) <= 0.15 * target)
        candidate = _round5(measured if confident else target)
        # Enforce strictly descending in 5-yard steps: never emit a club carrying
        # >= the one above it. If the candidate would break order, step down by 5.
        if prev_carry is not None and candidate >= prev_carry:
            suggested = prev_carry - 5
            held = True
        else:
            suggested = candidate
            held = not confident
        prev_carry = suggested
        bag.append({
            "club": name, "category": cat, "group": GROUP_OF.get(cat, "Other"),
            "target": target, "measured": measured, "suggested": suggested,
            "n": n or 0, "held": held, "low_conf": (n or 0) < 5 and not lm,
            "measured_src": lm,   # True = launch-monitor measured
        })

    # ---- lateral offsets per club (one pass; shared by dispersion + aim) ----
    pin_of = {}
    for h in holes:
        pin_of[(h.get("round_id"), h.get("hole_id"))] = (
            _f(h.get("pin_lat")), _f(h.get("pin_lng")))
    lat_by_club: dict[str, list[float]] = {}    # all shots — for dispersion spread
    for s in shots:
        if _truthy(s.get("is_putt")):
            continue
        club = s.get("club")
        if not club or club == "Putter":
            continue
        pin = pin_of.get((s.get("round_id"), s.get("hole_id")))
        off = _lateral_offset(
            (_f(s.get("start_lat")), _f(s.get("start_lng"))),
            (_f(s.get("end_lat")), _f(s.get("end_lng"))),
            pin if pin and all(pin) else (None, None))
        if off is not None and abs(off) < 80:  # drop wild geo glitches
            lat_by_club.setdefault(club, []).append(off)

    # ---- dispersion explorer (recency-weighted best-third, MC; wet-aware carry) ----
    bag_order = {name: i for i, (name, _t) in enumerate(target_bag)}
    disp_clubs = []
    for name, sh in shots_by_club.items():
        mc = club_mc.get(name)
        if not mc:
            continue
        med, lo, hi, n = mc                       # recency-weighted best-third (MC)
        cat = (club_meta.get(name, {}) or {}).get("club_category") or _club_cat(name)
        factor = club_factor.get(name, CARRY_FACTOR.get(cat, 0.97))   # wet-aware
        # spread = SD over the cleaned set (drop topped/chunked via carry floor)
        ds = sorted(d for d, _o, _w in sh)
        kept = [d for d in ds if d >= 0.8 * statistics.median(ds)] or ds
        lat = _iqr_filter(lat_by_club.get(name, []))   # drop hooks/pushes
        # PREFER a launch-monitor (measured) carry over the modeled one
        lm = name in lm_carry
        carry = _round5(lm_carry[name]) if lm else _round5(med * factor)
        disp_clubs.append({
            "club": name, "category": cat, "group": GROUP_OF.get(cat, "Other"),
            # total = real measured distance; carry = LM if measured, else wet-modeled
            "total": _round5(med), "total_lo": _round5(lo), "total_hi": _round5(hi),
            "carry": carry, "measured_src": lm, "wet": club_wet.get(name, 0.0),
            # spread = SD of the club's shots (shot-to-shot consistency)
            "carry_sd": _round5(statistics.pstdev(kept) * factor) if len(kept) > 1 else 0,
            "lateral_sd": _round5(statistics.pstdev(lat)) if len(lat) > 1 else None,
            "n": n,
            "confidence": "high" if n >= 12 else "medium" if n >= 6 else "low",
        })
    # Order by the player's bag if they have one; otherwise (no bag.csv) fall back to
    # the canonical club order — never leave clubs in hash order.
    disp_clubs.sort(key=lambda d: bag_order.get(d["club"], 1000 + _club_rank(d["club"])))

    # aim-by-club is derived from APPROACH shots only (vs green center) — see the
    # `patterns` block below; building it here as a placeholder for ordering.
    aim = []

    # ---- sga bands (approach / putting / short detail + goals) ----
    band_rows = {}
    for b in bands:
        band_rows.setdefault(b.get("section"), []).append({
            "metric": b.get("metric"), "slab": b.get("slab"),
            "unit": b.get("slab_unit"), "terrain": b.get("terrain"),
            "sga": _f(b.get("sga")), "n": _f(b.get("shots_count")),
            "avg_dist": _f(b.get("avg_dist_to_pin")),
            "dist_unit": b.get("dist_to_pin_unit"), "goal": _f(b.get("goal")),
        })

    # ---- trouble holes (worst avg score_to_par) ----
    by_hole: dict[str, list[float]] = {}
    holelen: dict[str, float] = {}
    holepar: dict[str, int] = {}
    for h in holes:
        hid = h.get("hole_id")
        by_hole.setdefault(hid, []).append(_f(h.get("score_to_par")))
        holelen[hid] = _f(h.get("hole_len_yd"))
        holepar[hid] = _i(h.get("par"))
    trouble = []
    for hid, vals in by_hole.items():
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        trouble.append({"hole": _i(hid), "par": holepar.get(hid),
                        "len": holelen.get(hid),
                        "avg_to_par": round(statistics.fmean(vals), 2),
                        "n": len(vals)})
    trouble.sort(key=lambda t: (-t["avg_to_par"], t["hole"] or 0))

    # ---- cost of misses (SG lost by category; combined w/ 0.62 efficiency) ----
    cat_sg = {
        "Off the tee": sg_arccos.get("drivingSga"),
        "Approach": sg_arccos.get("approachSga"),
        "Short game": sg_arccos.get("shortSga"),
        "Putting": sg_arccos.get("puttingSga"),
    }
    levers = sorted(
        ({"name": k, "sg": v} for k, v in cat_sg.items() if v is not None),
        key=lambda x: x["sg"])
    raw_recoverable = -sum(min(0, x["sg"]) for x in levers)
    eff_recoverable = round(raw_recoverable * 0.62, 1)

    # ---- green centers (centroid of a hole's pins across rounds) = aim proxy ----
    round_course = {h.get("round_id"): h.get("course") for h in holes}
    _gc: dict = {}
    for h in holes:
        la, ln = _f(h.get("pin_lat")), _f(h.get("pin_lng"))
        if la is not None and ln is not None:
            _gc.setdefault((h.get("course"), h.get("hole_id")), []).append((la, ln))
    green_center = {k: (statistics.fmean([p[0] for p in v]),
                        statistics.fmean([p[1] for p in v])) for k, v in _gc.items()}

    def _green_for(rid, hid):
        return green_center.get((round_course.get(rid), hid))

    # ---- shot patterns: 2D miss (short/long x left/right) vs GREEN CENTER ----
    bag_ord = {n: i for i, (n, _t) in enumerate(target_bag)}

    def _agg(ms):
        # Drop clear outliers (chunks/blades/shanks/GPS glitches) via 1.5*IQR on each
        # axis, then report the MEDIAN (middle of the distribution, robust) — same
        # spirit as the distance/dispersion cleaning elsewhere.
        if not ms:
            return None
        alo, ahi = _iqr_bounds([x[0] for x in ms])
        blo, bhi = _iqr_bounds([x[1] for x in ms])
        kept = [(x, y) for x, y in ms if alo <= x <= ahi and blo <= y <= bhi] or ms
        a = [p[0] for p in kept]
        b = [p[1] for p in kept]
        return {"n": len(ms), "used": len(kept), "dropped": len(ms) - len(kept),
                "ls": round(statistics.median(a), 1), "lr": round(statistics.median(b), 1),
                "ls_sd": round(statistics.pstdev(a), 1) if len(a) > 1 else 0,
                "lr_sd": round(statistics.pstdev(b), 1) if len(b) > 1 else 0}

    def _pattern(category, min_n=4, rounds_set=None):
        pts, by = [], {}
        for s in shots:
            if (s.get("category_approx") or "") != category:
                continue
            if rounds_set is not None and s.get("round_id") not in rounds_set:
                continue
            tgt = _green_for(s.get("round_id"), s.get("hole_id"))
            st = (_f(s.get("start_lat")), _f(s.get("start_lng")))
            en = (_f(s.get("end_lat")), _f(s.get("end_lng")))
            if not tgt or None in tgt or None in st or None in en:
                continue
            m = _miss_vs(st, en, tgt)
            if not m or abs(m[0]) > 80 or abs(m[1]) > 80:
                continue
            club = s.get("club") or ""
            pts.append({"club": club, "ls": round(m[0], 1), "lr": round(m[1], 1),
                        "cat": (club_meta.get(club, {}) or {}).get("club_category")
                               or _club_cat(club)})
            by.setdefault(club, []).append(m)
        by_club = []
        for c, ms in by.items():
            if len(ms) >= min_n:
                a = _agg(ms)
                a["club"] = c
                by_club.append(a)
        by_club.sort(key=lambda x: bag_ord.get(x["club"], 1000 + _club_rank(x["club"])))
        return {"points": pts, "by_club": by_club,
                "overall": _agg([(p["ls"], p["lr"]) for p in pts]) if pts else None}

    patterns = {"approach": _pattern("approach"), "short": _pattern("short_game", 3)}

    # ---- aim-by-club: from APPROACH shots only (aimed at green center) ----
    # Red-team fix: blending tee/layup shots (aimed down the fairway) faked right
    # misses on clubs the player only hooks. Approach-only is the clean signal.
    for c in patterns["approach"]["by_club"]:
        bias, nb = c["lr"], c["n"]
        if nb < 6:
            rec = "need more data"
        elif abs(bias) >= 5:
            rec = "aim %s" % ("left" if bias > 0 else "right")
        else:
            rec = "on line"
        aim.append({"club": c["club"], "bias": bias, "n": nb,
                    "side": "right" if bias > 0 else "left", "rec": rec})

    # ---- driving accuracy (fairway hit / miss L-R by tee club) ----
    # Woods/driver are aimed down the fairway, not at the green — so direction off the
    # tee is measured by Arccos's fairway-hit + miss-side flags, not vs green center.
    tee_club = {}
    for s in shots:
        if _truthy(s.get("is_tee")):
            tee_club[(s.get("round_id"), s.get("hole_id"))] = s.get("club")
    dacc: dict = {}
    for h in holes:
        if (_i(h.get("par")) or 0) < 4:
            continue                       # par 3s have no fairway off the tee
        tc = tee_club.get((h.get("round_id"), h.get("hole_id"))) or "?"
        a = dacc.setdefault(tc, [0, 0, 0, 0])   # [hit, left, right, chances]
        a[3] += 1
        if _truthy(h.get("fairway_hit")):
            a[0] += 1
        elif _truthy(h.get("fw_miss_left")):
            a[1] += 1
        elif _truthy(h.get("fw_miss_right")):
            a[2] += 1
    driving = []
    for c, a in dacc.items():
        if a[3] >= 3 and c and c != "?":
            driving.append({"club": c, "chances": a[3],
                            "fw_pct": round(100 * a[0] / a[3]),
                            "left_pct": round(100 * a[1] / a[3]),
                            "right_pct": round(100 * a[2] / a[3])})
    driving.sort(key=lambda x: bag_ord.get(x["club"], 1000 + _club_rank(x["club"])))

    # ---- pace of play (round duration) ----
    def _dur_min(s):
        try:
            parts = [int(x) for x in str(s).split(":")]
            return parts[0] * 60 + parts[1] + (parts[2] / 60 if len(parts) > 2 else 0)
        except (ValueError, TypeError, IndexError):
            return None
    pace = []
    for r in rounds:
        mins = _dur_min(r.get("pace_of_play"))
        hl = _i(r.get("holes")) or 18
        if mins:
            per18 = mins / hl * 18
            pace.append({"date": r.get("date"), "course": r.get("course"),
                         "holes": hl, "minutes": round(mins),
                         "per18_min": round(per18)})
    pace.sort(key=lambda x: x["date"] or "")
    pace_avg18 = round(statistics.fmean([p["per18_min"] for p in pace])) if pace else None

    # ---- putting: one-putt % + 3-putts by first-putt distance band ----
    PB = [("0-3 ft", 0, 1), ("3-6 ft", 1, 2), ("6-10 ft", 2, 10 / 3),
          ("10-20 ft", 10 / 3, 20 / 3), ("20-30 ft", 20 / 3, 10), ("30+ ft", 10, 1e9)]
    pholes: dict = {}
    for s in shots:
        if _truthy(s.get("is_putt")):
            pholes.setdefault((s.get("round_id"), s.get("hole_id")), []).append(s)
    pacc = {b[0]: {"made": 0, "att": 0, "tp": 0} for b in PB}
    for key, ps in pholes.items():
        ps.sort(key=lambda s: _i(s.get("shot_num")) or 0)
        d_ft = (_f(ps[0].get("start_dist_to_pin_yd")) or 0) * 3
        band = next((b[0] for b in PB if b[1] <= d_ft < b[2]), PB[-1][0])
        pacc[band]["att"] += 1
        if len(ps) == 1:
            pacc[band]["made"] += 1
        if len(ps) >= 3:
            pacc[band]["tp"] += 1
    putting_dist = [{"band": b[0], "made": pacc[b[0]]["made"], "att": pacc[b[0]]["att"],
                     "make_pct": round(100 * pacc[b[0]]["made"] / pacc[b[0]]["att"])
                     if pacc[b[0]]["att"] else 0, "tp": pacc[b[0]]["tp"]}
                    for b in PB if pacc[b[0]]["att"]]

    # ---- up & down by lie (from Arccos career rates) ----
    cby = career.get("career_by_category", {})
    updown = [
        {"lie": "Chip (around green)",
         "made": (cby.get("chip", {}) or {}).get("noOfChipSaveSuccesses"),
         "att": (cby.get("chip", {}) or {}).get("noOfChipSaveChances")},
        {"lie": "Sand (bunker)",
         "made": (cby.get("sand", {}) or {}).get("noOfSandSaveSuccesses"),
         "att": (cby.get("sand", {}) or {}).get("noOfSandSaveChances")},
    ]

    # ================= COACHING ENGINE: macro vs recent + per club =============
    rsorted = sorted(rounds, key=lambda r: r.get("date") or "")
    n_recent = min(2, len(rsorted))
    recent_ids = {r.get("round_id") for r in rsorted[-n_recent:]}
    recent_rows = [r for r in rsorted[-n_recent:]]
    recent_label = (f'{recent_rows[0].get("date")}'
                    + (f' – {recent_rows[-1].get("date")}' if n_recent > 1 else '')
                    ) if recent_rows else ""

    def _avg(rows, key):
        vals = [_f(r.get(key)) for r in rows if _f(r.get(key)) is not None]
        return round(statistics.fmean(vals), 1) if vals else None

    SGCATS = [("Off the tee", "sg_off_tee_arccos"), ("Approach", "sg_approach_arccos"),
              ("Short game", "sg_short_arccos"), ("Putting", "sg_putting_arccos")]
    sg_compare = []
    for nm, key in SGCATS:
        mac, rec = _avg(rsorted, key), _avg(recent_rows, key)
        sg_compare.append({"cat": nm, "macro": mac, "recent": rec,
                           "delta": round(rec - mac, 1) if (rec is not None and mac is not None) else None})

    appr_recent = _pattern("approach", min_n=10 ** 9, rounds_set=recent_ids)["overall"]

    def _fw_stats(rids):
        hit = tot = le = ri = 0
        for h in holes:
            if (_i(h.get("par")) or 0) < 4:
                continue
            if rids is not None and h.get("round_id") not in rids:
                continue
            tot += 1
            if _truthy(h.get("fairway_hit")):
                hit += 1
            elif _truthy(h.get("fw_miss_left")):
                le += 1
            elif _truthy(h.get("fw_miss_right")):
                ri += 1
        return {"fw": round(100 * hit / tot) if tot else None, "tot": tot}
    fw_macro, fw_recent = _fw_stats(None), _fw_stats(recent_ids)

    # ---- macro recommendations (persistent, ranked by leverage) ----
    ov = patterns["approach"]["overall"]
    drv = next((x for x in driving if x["club"] == "Driver"), None)
    tp_total = sum(p["tp"] for p in putting_dist)
    chip_ud = updown[0]

    # Systemic directional pattern: a consistent one-side miss ACROSS clubs is a
    # face/path swing tendency, not N separate aim errors. Count clubs leaning each way
    # (approach medians, vs green center) plus the driver's fairway-miss side.
    _bc = patterns["approach"]["by_club"]
    left_bag = [c["club"] for c in _bc if c["lr"] <= -4]
    right_bag = [c["club"] for c in _bc if c["lr"] >= 4]
    drv_left = bool(drv and drv["left_pct"] - drv["right_pct"] >= 10)
    drv_right = bool(drv and drv["right_pct"] - drv["left_pct"] >= 10)
    nL = len(left_bag) + (1 if drv_left else 0)
    nR = len(right_bag) + (1 if drv_right else 0)
    hook_side = "left" if right_handed else "right"
    swing = None
    if nL >= 3 and nL >= nR * 2:
        swing = {"side": "left", "n": nL, "drv": drv_left,
                 **_swing_fault("left", right_handed)}
    elif nR >= 3 and nR >= nL * 2:
        swing = {"side": "right", "n": nR, "drv": drv_right,
                 **_swing_fault("right", right_handed)}

    # ---- recent form: extra flags beyond the per-category trend cards ----
    recent_items = []
    if appr_recent and ov and appr_recent["ls"] - ov["ls"] <= -4:
        recent_items.append({
            "head": "Approaches shorter than usual lately",
            "body": f"median {abs(appr_recent['ls'])} yd short recently vs {abs(ov['ls'])} macro — "
            "double-check your club selection in current conditions."})
    if fw_recent["fw"] is not None and fw_macro["fw"] is not None and fw_recent["fw"] - fw_macro["fw"] <= -10:
        recent_items.append({
            "head": f"Driving accuracy dipped ({fw_recent['fw']}% fairways lately vs {fw_macro['fw']}%)",
            "body": "recent tee-ball control off — favor a club you can find the short grass with."})

    # ---- per-club coaching (macro/structural) ----
    pat_bc = {c["club"]: c for c in patterns["approach"]["by_club"]}
    drv_bc = {x["club"]: x for x in driving}
    club_recs = []
    for name, _t in target_bag:
        dd, pc = drv_bc.get(name), pat_bc.get(name)
        if dd and dd["chances"] >= 3:
            le, ri, fw = dd["left_pct"], dd["right_pct"], dd["fw_pct"]
            if le - ri >= 12:
                miss_side, miss_pct, other_pct = "left", le, ri
            elif ri - le >= 12:
                miss_side, miss_pct, other_pct = "right", ri, le
            else:
                miss_side = None
            if miss_side:
                aim_side = _opposite_side(miss_side)
                miss_abbr = _side_abbr(miss_side)
                other_abbr = _side_abbr(aim_side)
                if miss_side == hook_side:
                    d_txt = (f"hooks {miss_side} ({miss_pct}% {miss_abbr} vs "
                             f"{other_pct}% {other_abbr}) — aim down the {aim_side} "
                             "side, play the draw")
                else:
                    d_txt = (f"leaks {miss_side} ({miss_pct}% {miss_abbr}) — "
                             f"aim {aim_side} / check the face")
            else:
                d_txt = f"two-way miss ({le}% L / {ri}% R) — pick one side and commit"
            club_recs.append({"club": name, "rec": f"{fw}% fairways; {d_txt}.",
                              "basis": f"{dd['chances']} tee shots"})
        elif pc:
            ls, lr = pc["ls"], pc["lr"]
            dist = (f"Club up (~{abs(ls)}y short)" if ls <= -8 else
                    f"Lean to more club (~{abs(ls)}y short)" if ls <= -4 else
                    f"Club down (~{ls}y long)" if ls >= 8 else "Distance dialed")
            direc = (f"aim ~{abs(lr)}y right" if lr <= -8 else
                     "start it a touch right" if lr <= -4 else
                     f"aim ~{lr}y left" if lr >= 8 else "on line")
            club_recs.append({"club": name, "rec": f"{dist}; {direc}.",
                              "basis": f"{pc['used']} approaches"})
    if tp_total:
        club_recs.append({"club": "Putter",
                          "rec": "Lag drill — long putts to a 3-ft circle; nearly all "
                          "3-putts come from 30+ ft.", "basis": "putting"})

    # ---- Top 10 actions ranked by estimated strokes gained ----
    # Each action captures a fraction of a category's per-round SG loss. The category
    # losses are YOUR data (so the numbers scale as you play); the fractions are golf-
    # domain judgment for how much of that loss the action addresses. Read as the
    # upside if you close that gap toward a mid-handicap baseline.
    _loss = {x["name"]: -x["sg"] for x in levers if x["sg"] < 0}
    A = _loss.get("Approach", 0.0)
    P = _loss.get("Putting", 0.0)
    T = _loss.get("Off the tee", 0.0)
    S = _loss.get("Short game", 0.0)
    gir = kr.get("gir_pct")
    _ov = ov or {"ls": 0, "lr": 0}     # safe refs even on an empty store
    cand = []

    def _act(sg, cat, action, detail):
        if sg and sg > 0.05:
            cand.append({"sg": round(sg, 1), "cat": cat, "action": action, "detail": detail})

    if swing:
        if swing["fault"] == "hook":
            _hookdet = (f"Your whole bag leaks {swing['side']} ({swing['n']} clubs"
                        + (", driver included" if swing["drv"] else "")
                        + ") — a face-to-path issue, the root cause behind both lost fairways "
                        "and missed greens. Squaring the face fixes every club at once; the "
                        "per-club “aim” notes are just patches until you do. Nothing else "
                        "here moves more strokes.")
        else:
            _hookdet = (f"Your whole bag leaks {swing['side']} ({swing['n']} clubs"
                        + (", driver included" if swing["drv"] else "")
                        + ") — a face-to-path slice/push, with the face open relative "
                        "to path. Squaring the face fixes every club at once; the "
                        "per-club “aim” notes are just patches until you do. Nothing else "
                        "here moves more strokes.")
        if lm_bag_f2p is not None:
            _fd = swing["face_past"]
            _hookdet += (f" Measured: face averages {abs(lm_bag_f2p)}° {_fd} to the "
                         "path — get it toward 0°.")
        _act(0.18 * A + 0.45 * T, "Swing",
             f"Fix the face-to-path {swing['fault']} (lesson + launch monitor)", _hookdet)
    _act(0.28 * A, "Approach", "Club up — stop leaving approaches short",
         f"GIR is only {_pct(gir, 0)} and you finish a median {abs(_ov['ls'])} yd short. "
         "One more club turns 'just short' into birdie looks — your biggest pure-stats gain.")
    _act(0.55 * P, "Putting", f"Lag drill — kill the 3-putts (all {tp_total} from 30+ ft)",
         "Long putts to a 3-ft circle. It's a distance-control problem, not your stroke; "
         "shrinks fast and compounds as approach proximity improves.")
    _act(0.20 * A, "Approach", "Sharpen the wedges (50–125 yd scoring clubs)",
         "Tight distance control here is where mid-handicaps make birdies. Build a "
         "carry ladder (¾, full) once you have launch-monitor numbers.")
    if chip_ud.get("att") and (chip_ud.get("made") or 0) / chip_ud["att"] < 0.25:
        _act(S, "Short game",
             f"Greenside contact — stop chunking chips ({chip_ud['made']}/{chip_ud['att']} up-and-down)",
             "A strike/technique fix (lesson + a low, running bump-and-run). Goal: solid "
             "contact to inside 15 ft so the putt is makeable.")
    _act(0.14 * A, "Approach", "Re-gap the long irons / hybrid",
         "5i and hybrid come up ~20–40 yd short on course — get real carries off a launch "
         "monitor and trust the number, or replace them with clubs you can hit the green with.")
    tee_miss_side = ("left" if drv_left else "right" if drv_right else
                     swing["side"] if swing else hook_side)
    tee_play = "hook" if tee_miss_side == hook_side else "fade"
    tee_aim_side = _opposite_side(tee_miss_side)
    _act(0.32 * T, "Off the tee", f"Tee strategy — play for the {tee_play}",
         f"Driver finds {drv['fw_pct'] if drv else 0}% fairways. Aim down the {tee_aim_side} side, "
         "and take 3-wood on tight holes — managing the miss while you fix the swing.")
    _act(0.23 * P, "Putting", "Own the 4–8 footers",
         "The makeable range that swings scores. 10 minutes a session on a gate drill; "
         "more pars saved than any long-putt practice.")
    _act(0.10 * A, "Course mgmt", "Aim at green center, never the pin",
         "You already do this — keep it. Center-of-green removes short-side disasters and "
         "is worth real strokes for a mid-handicap.")
    _act(0.14 * P, "Putting", "Speed first on every lag",
         "On 30+ footers, prioritize finishing within 3 ft over the line. Speed kills "
         "3-putts more than aim does.")

    top10 = sorted(cand, key=lambda x: x["sg"], reverse=True)[:10]
    for i, t in enumerate(top10, 1):
        t["rank"] = i
    top10_total = round(sum(t["sg"] for t in top10), 1)

    coaching = {"recent_label": recent_label, "recent": recent_items,
                "by_club": club_recs, "sg_compare": sg_compare,
                "top10": top10, "top10_total": top10_total}
    # ===========================================================================

    # ---- per-round hole detail (for the full round-review pages) ----
    holes_by_round: dict[str, list[dict]] = {}
    for h in holes:
        holes_by_round.setdefault(h.get("round_id"), []).append({
            "hole": _i(h.get("hole_id")), "par": _i(h.get("par")),
            "len": _f(h.get("hole_len_yd")), "shots": _i(h.get("shots")),
            "to_par": _i(h.get("score_to_par")), "putts": _i(h.get("putts")),
            "fairway": _truthy(h.get("fairway_hit")), "gir": _truthy(h.get("gir")),
            "drive": _f(h.get("drive_yd")),
            "drive_club": tee_club.get((h.get("round_id"), h.get("hole_id"))),
            "proximity": _f(h.get("approach_proximity_yd")),
            "penalties": _i(h.get("penalties")),
            "sg": _f(h.get("sg_hole_broadie")),
        })
    for hid in holes_by_round:
        holes_by_round[hid].sort(key=lambda x: x["hole"] or 0)

    return {
        "meta": {
            "generated_at": (disp.get("generated_at")
                             or career.get("pulled_at") or ""),
            "n_rounds": len(rounds), "n_holes": len(holes), "n_shots": len(shots),
            "courses": sorted({r["course"] for r in round_rows if r["course"]}),
        },
        "player": {
            "index": idx,
            "home": (profile.get("homeCourse") or {}).get("name"),
            "n_posted": len(diffs),
        },
        "right_handed": right_handed,
        "kpis": {
            "index": idx,
            "gir_pct": kr.get("gir_pct"), "fairway_pct": kr.get("fairway_pct"),
            "putts_per_round": kr.get("putts_per_round"),
            "three_putt_pct": kr.get("three_putt_pct"),
            "chip_save_pct": kr.get("scramble_chip_save_pct"),
            "sand_save_pct": kr.get("sand_save_pct"),
            "chip_error_rate": kr.get("chip_error_rate"),
        },
        "sg_career": cat_sg,
        "rounds": round_rows,
        "bag": bag,
        # all bag.csv specs (incl. no-carry clubs like the putter, absent from target_bag)
        "bag_specs": list(bag_specs.values()),
        "dispersion": disp_clubs,
        "patterns": patterns, "putting_dist": putting_dist, "updown": updown,
        "driving": driving, "pace": pace, "pace_avg18": pace_avg18,
        "coaching": coaching,
        "aim": aim,
        "bands": band_rows,
        "trouble": trouble[:6],
        "levers": levers,
        "recoverable": {"raw": round(raw_recoverable, 1), "effective": eff_recoverable},
        "posted": [{
            "date": g.get("played_at"), "course": g.get("course_name"),
            "score": _i(g.get("adjusted_gross_score")), "holes": _i(g.get("holes")),
            "diff": _f(g.get("differential")), "used": _truthy(g.get("used")),
        } for g in sorted(ghin, key=lambda g: g.get("played_at") or "", reverse=True)],
        "career": career,
        "holes_by_round": holes_by_round,
        # map payload: per round -> per hole -> shot polyline
        "map": _map_payload(shots, holes),
        # per-shot detail (club/dist/lie/GPS) for the hole-by-hole shot explorer
        "shotmap": _shotmap_payload(shots, holes),
    }


def _club_cat(name: str) -> str:
    n = name.lower()
    if "driver" in n:
        return "Driver"
    if "wood" in n:
        return "Wood"
    if "hybrid" in n:
        return "Hybrid"
    if "iron" in n:
        return "Iron"
    if "wedge" in n:
        return "Wedge"
    if "putter" in n:
        return "Putter"
    return "Iron"


def _club_rank(name: str) -> int:
    """Canonical bag order for any club: Driver < woods < hybrids < irons < wedges
    (by loft) < putter. The fallback sort key for club tables when there's no bag.csv,
    so every table orders the same sensible way instead of hash order."""
    n = (name or "").strip().lower()
    if "driver" in n:
        return 0
    if "putter" in n:
        return 900
    digits = "".join(c if c.isdigit() else " " for c in n).split()
    k = int(digits[0]) if digits else 0
    if "wood" in n:
        return 100 + k          # 3W (103) before 5W (105)
    if "hybrid" in n:
        return 200 + k
    if "iron" in n:
        return 300 + k          # 3i .. 9i
    if "wedge" in n:
        if "pitch" in n or n == "pw":
            return 400
        if "gap" in n or "approach" in n or n in ("gw", "aw"):
            return 448
        if "sand" in n or n == "sw":
            return 454
        if "lob" in n or n == "lw":
            return 460
        if 40 <= k <= 64:
            return 400 + k      # "54 Wedge" -> 454
        return 449
    return 500 + k


def _map_payload(shots, holes):
    rounds: dict[str, dict] = {}
    for h in holes:
        rid = h.get("round_id")
        rounds.setdefault(rid, {"date": h.get("date"), "course": h.get("course"),
                                "holes": {}})
    for s in shots:
        rid = s.get("round_id")
        sl, sg = _f(s.get("start_lat")), _f(s.get("start_lng"))
        el, eg = _f(s.get("end_lat")), _f(s.get("end_lng"))
        if sl is None or sg is None:
            continue
        rd = rounds.setdefault(rid, {"date": s.get("date"), "holes": {}})
        hole = rd["holes"].setdefault(s.get("hole_id"), [])
        pt = [round(sl, 6), round(sg, 6)]
        if not hole or hole[-1] != pt:
            hole.append(pt)
        if el is not None and eg is not None:
            hole.append([round(el, 6), round(eg, 6)])
    # to list form, drop rounds without any geo
    out = []
    for rid, rd in rounds.items():
        holelist = [{"hole": hid, "pts": pts}
                    for hid, pts in sorted(rd.get("holes", {}).items(),
                                           key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)
                    if pts]
        if holelist:
            out.append({"round_id": rid, "date": rd.get("date"),
                        "course": rd.get("course"), "holes": holelist})
    out.sort(key=lambda r: r.get("date") or "")
    return out


def _shotmap_payload(shots, holes):
    """Per round -> per hole -> per shot, with club + distance + lie + GPS, for the
    hole-by-hole shot explorer. Keeps each shot so the map can label the club."""
    hole_meta = {}
    for h in holes:
        hole_meta[(h.get("round_id"), h.get("hole_id"))] = {
            "par": _i(h.get("par")), "len": _f(h.get("hole_len_yd")),
            "pin": [_f(h.get("pin_lat")), _f(h.get("pin_lng"))],
        }
    rounds: dict[str, dict] = {}
    for s in shots:
        sl, sg = _f(s.get("start_lat")), _f(s.get("start_lng"))
        if sl is None or sg is None:
            continue
        el, eg = _f(s.get("end_lat")), _f(s.get("end_lng"))
        rid, hid = s.get("round_id"), s.get("hole_id")
        rd = rounds.setdefault(rid, {})
        rd.setdefault(hid, []).append({
            "n": _i(s.get("shot_num")), "club": s.get("club") or "",
            "cat": s.get("club_category") or _club_cat(s.get("club") or ""),
            "dist": round(_f(s.get("shot_distance_yd")) or 0),
            "lie": s.get("lie_approx") or "", "putt": _truthy(s.get("is_putt")),
            "tee": _truthy(s.get("is_tee")),
            "dtp_s": round(_f(s.get("start_dist_to_pin_yd")) or 0),
            "dtp_e": round(_f(s.get("end_dist_to_pin_yd")) or 0),
            "s": [round(sl, 6), round(sg, 6)],
            "e": [round(el, 6), round(eg, 6)] if el is not None and eg is not None else None,
        })
    out = {}
    for rid, hl in rounds.items():
        holelist = []
        for hid, sh in sorted(hl.items(),
                              key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
            sh.sort(key=lambda x: x["n"] or 0)
            meta = hole_meta.get((rid, hid), {})
            holelist.append({"hole": _i(hid), "par": meta.get("par"),
                             "len": meta.get("len"), "pin": meta.get("pin"),
                             "shots": sh})
        out[rid] = holelist
    return out


# ----------------------------------------------------------------- charts (SVG)
def _nice_ticks(lo, hi, step):
    """Integer tick values at multiples of `step` spanning [lo, hi]."""
    start = math.ceil(lo / step) * step
    out, v = [], start
    while v <= hi + 1e-6:
        out.append(int(round(v)))
        v += step
    return out


def _svg_sg_by_round(rounds) -> str:
    """Inline SVG stacked-bar of SG categories per round, with title + y-axis."""
    if not rounds:
        return ""
    cats = [("sg_off_tee", "var(--green-deep)", "off tee"),
            ("sg_approach", "var(--green)", "approach"),
            ("sg_short", "var(--brass)", "short"),
            ("sg_putting", "var(--ink-soft)", "putting")]
    W, H, padL, padR, padT, padB = 700, 290, 42, 14, 46, 36
    n = len(rounds)
    pos = [sum((r.get(k) or 0) for k, _c, _l in cats if (r.get(k) or 0) > 0) for r in rounds]
    neg = [sum((r.get(k) or 0) for k, _c, _l in cats if (r.get(k) or 0) < 0) for r in rounds]
    ymax = math.ceil(max(pos + [1]) / 5) * 5
    ymin = math.floor(min(neg + [-1]) / 5) * 5
    span = (ymax - ymin) or 1
    plotH = H - padT - padB

    def yf(v):
        return padT + (ymax - v) / span * plotH

    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
         f'style="width:100%;height:auto;background:var(--paper);border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)">']
    p.append(f'<text x="{W/2:.0f}" y="16" font-size="12" font-weight="bold" '
             f'fill="var(--ink)" text-anchor="middle">Strokes gained by round (vs scratch)</text>')
    # y gridlines + tick labels (SG values)
    for yv in _nice_ticks(ymin, ymax, 5):
        y = yf(yv)
        p.append(f'<line x1="{padL}" y1="{y:.1f}" x2="{W-padR}" y2="{y:.1f}" '
                 f'stroke="{"var(--ink)" if yv == 0 else "var(--rule)"}"/>')
        p.append(f'<text x="{padL-5}" y="{y+3:.1f}" font-size="9" fill="var(--ink-soft)" '
                 f'text-anchor="end">{yv:+d}</text>')
    p.append(f'<text transform="translate(11,{(padT+H-padB)/2:.0f}) rotate(-90)" '
             f'font-size="9" fill="var(--ink-soft)" text-anchor="middle">strokes gained</text>')
    step = (W - padL - padR) / n
    bw = min(46, step * 0.5)
    for i, r in enumerate(rounds):
        cx = padL + step * i + step / 2
        up = dn = 0.0
        for k, color, _l in cats:
            v = r.get(k) or 0
            if v >= 0:
                y1, h = yf(up + v), yf(up) - yf(up + v)
                up += v
            else:
                y1, h = yf(dn), yf(dn + v) - yf(dn)
                dn += v
            if h > 0.4:
                p.append(f'<rect x="{cx-bw/2:.1f}" y="{y1:.1f}" width="{bw:.1f}" '
                         f'height="{h:.1f}" fill="{color}"/>')
        p.append(f'<text x="{cx:.1f}" y="{yf(pos[i])-4:.1f}" font-size="10" '
                 f'fill="var(--ink)" text-anchor="middle">{_num(r.get("sg_total"),1,True)}</text>')
        p.append(f'<text x="{cx:.1f}" y="{H-padB+14}" font-size="9" fill="var(--ink-soft)" '
                 f'text-anchor="middle">{_esc(r["date"])}</text>')
    lx = padL
    for _k, color, lbl in cats:
        p.append(f'<rect x="{lx}" y="26" width="9" height="9" rx="2" fill="{color}"/>')
        p.append(f'<text x="{lx+13}" y="34" font-size="9.5" fill="var(--ink-soft)">{lbl}</text>')
        lx += 95
    p.append("</svg>")
    return "".join(p)


def _svg_dispersion(disp_clubs) -> str:
    """Inline SVG scatter of carry (x, yds) vs lateral spread (y, yds), with ticks."""
    pts = [(d["carry"], d["lateral_sd"], d["club"], d["confidence"])
           for d in disp_clubs if d["carry"] and d["lateral_sd"]]
    if not pts:
        return ""
    cmap = {"high": "var(--green-deep)", "medium": "var(--brass)", "low": "var(--ink-soft)"}
    W, H, padL, padR, padT, padB = 700, 330, 50, 16, 40, 44
    xs = [c for c, _l, _n, _cf in pts]
    ys = [l for _c, l, _n, _cf in pts]
    xmin = math.floor((min(xs) - 15) / 25) * 25
    xmax = math.ceil((max(xs) + 15) / 25) * 25
    ymax = max(math.ceil((max(ys) + 5) / 10) * 10, 10)
    pw, ph = W - padL - padR, H - padT - padB

    def xf(v):
        return padL + (v - xmin) / ((xmax - xmin) or 1) * pw

    def yf(v):
        return padT + (ymax - v) / (ymax or 1) * ph

    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
         f'style="width:100%;height:auto;background:var(--paper);border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)">']
    p.append(f'<text x="{W/2:.0f}" y="16" font-size="12" font-weight="bold" '
             f'fill="var(--ink)" text-anchor="middle">Carry distance vs lateral spread '
             f'(lower = tighter)</text>')
    # x gridlines + tick labels (carry yards)
    for xv in _nice_ticks(xmin, xmax, 50):
        x = xf(xv)
        p.append(f'<line x1="{x:.1f}" y1="{padT}" x2="{x:.1f}" y2="{H-padB}" stroke="var(--rule)"/>')
        p.append(f'<text x="{x:.1f}" y="{H-padB+15}" font-size="9" fill="var(--ink-soft)" '
                 f'text-anchor="middle">{xv}</text>')
    # y gridlines + tick labels (spread yards)
    for yv in _nice_ticks(0, ymax, 10):
        y = yf(yv)
        p.append(f'<line x1="{padL}" y1="{y:.1f}" x2="{W-padR}" y2="{y:.1f}" stroke="var(--rule)"/>')
        p.append(f'<text x="{padL-6}" y="{y+3:.1f}" font-size="9" fill="var(--ink-soft)" '
                 f'text-anchor="end">{yv}</text>')
    p.append(f'<text x="{(padL+W-padR)/2:.0f}" y="{H-6}" font-size="9.5" fill="var(--ink-soft)" '
             f'text-anchor="middle">carry (yds)</text>')
    p.append(f'<text transform="translate(12,{(padT+H-padB)/2:.0f}) rotate(-90)" '
             f'font-size="9.5" fill="var(--ink-soft)" text-anchor="middle">lateral spread ±SD (yds)</text>')
    for carry, lat, club, conf in pts:
        x, y = xf(carry), yf(lat)
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{cmap.get(conf,"var(--ink-soft)")}"/>')
        p.append(f'<text x="{x+7:.1f}" y="{y+3:.1f}" font-size="9" '
                 f'fill="var(--ink)">{_esc(club)}</text>')
    lx = W - padR - 200
    for k, color in cmap.items():
        p.append(f'<circle cx="{lx}" cy="30" r="4" fill="{color}"/>')
        p.append(f'<text x="{lx+8}" y="33" font-size="9" fill="var(--ink-soft)">{k}</text>')
        lx += 65
    p.append("</svg>")
    return "".join(p)


def _svg_pattern(pts, overall, title, by_club=None, pid="pat") -> str:
    """Green-relative shot scatter: target (green center) at origin, each dot a shot's
    finish — up=long, down=short, left/right = left/right. Dots are class-tagged by
    club ({pid}-dot-{clubid}) and each club's average-miss marker is rendered hidden,
    so a dropdown can filter to one club. Pure stdlib SVG."""
    if not pts:
        return ""
    catcol = {"Driver": "var(--green-deep)", "Wood": "var(--brass)",
              "Hybrid": "var(--brass-soft)", "Iron": "var(--green)",
              "Wedge": "var(--ink-soft)", "Putter": "var(--ink)"}
    W = Hh = 440
    cx, cy = W / 2, Hh / 2 + 8
    rmax = max([abs(p["lr"]) for p in pts] + [abs(p["ls"]) for p in pts] + [15])
    rmax = math.ceil(rmax / 10) * 10
    R = min(W, Hh) / 2 - 40

    def X(lr):
        return cx + lr / rmax * R

    def Y(ls):
        return cy - ls / rmax * R

    p = [f'<svg viewBox="0 0 {W} {Hh}" xmlns="http://www.w3.org/2000/svg" '
         f'style="width:100%;max-width:440px;height:auto;background:var(--paper);border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)">']
    p.append(f'<text x="{cx:.0f}" y="16" font-size="12" font-weight="bold" '
             f'fill="var(--ink)" text-anchor="middle">{_esc(title)}</text>')
    # range rings
    for r in range(10, int(rmax) + 1, 10):
        rr = r / rmax * R
        p.append(f'<circle cx="{cx}" cy="{cy}" r="{rr:.1f}" fill="none" stroke="var(--rule)"/>')
        p.append(f'<text x="{cx:.0f}" y="{cy-rr-2:.1f}" font-size="8" fill="var(--ink-soft)" '
                 f'text-anchor="middle">{r}yd</text>')
    # crosshair + direction labels
    p.append(f'<line x1="{cx}" y1="{cy-R}" x2="{cx}" y2="{cy+R}" stroke="var(--ink-soft)"/>')
    p.append(f'<line x1="{cx-R}" y1="{cy}" x2="{cx+R}" y2="{cy}" stroke="var(--ink-soft)"/>')
    for txt, x, y, anc in [("long", cx, cy - R - 4, "middle"),
                           ("short", cx, cy + R + 12, "middle"),
                           ("left", cx - R - 4, cy - 4, "end"),
                           ("right", cx + R + 4, cy - 4, "start")]:
        p.append(f'<text x="{x:.0f}" y="{y:.0f}" font-size="9" fill="var(--ink-soft)" '
                 f'text-anchor="{anc}">{txt}</text>')
    # shots (class-tagged by club for the dropdown filter)
    for pt in pts:
        p.append(f'<circle class="{pid}-dot {pid}-dot-{_cid(pt["club"])}" '
                 f'cx="{X(pt["lr"]):.1f}" cy="{Y(pt["ls"]):.1f}" r="3" '
                 f'fill="{catcol.get(pt["cat"], "var(--ink-soft)")}" opacity="0.82"/>')
    # green-center target
    p.append(f'<circle cx="{cx}" cy="{cy}" r="4" fill="var(--paper)" stroke="var(--ink)" stroke-width="1.5"/>')

    def _avg_marker(lr, ls, klass, style=""):
        mx, my = X(lr), Y(ls)
        return (f'<g class="{klass}"{style}><circle cx="{mx:.1f}" cy="{my:.1f}" r="7" '
                f'fill="none" stroke="var(--ink)" stroke-width="2"/>'
                f'<text x="{mx+10:.1f}" y="{my+3:.1f}" font-size="9" fill="var(--ink)">median</text></g>')

    # overall avg (shown by default) + a hidden avg per club (shown when filtered)
    if overall:
        p.append(_avg_marker(overall["lr"], overall["ls"], f'{pid}-avg {pid}-avg-all'))
    for c in (by_club or []):
        klass = f'{pid}-avg {pid}-avg-{_cid(c["club"])}'
        p.append(_avg_marker(c["lr"], c["ls"], klass, ' style="display:none"'))
    p.append("</svg>")
    return "".join(p)


# --------------------------------------------------------------------- render
def _pct(v, d=0):
    return "—" if v is None else f"{v:.{d}f}%"


def _num(v, d=1, plus=False):
    if v is None:
        return "—"
    s = f"{v:+.{d}f}" if plus else f"{v:.{d}f}"
    return s


def _esc(s):
    return html.escape(str(s)) if s is not None else ""


def render_html(d: dict, font_prefix: str = "assets/fonts") -> str:
    font_prefix = font_prefix.rstrip("/")
    p, k, m = d["player"], d["kpis"], d["meta"]

    # ---- game plan (derived from the data, calibrated to player context) ----
    levers = d["levers"]
    worst = levers[0]["name"] if levers else "short game"
    plan = [
        f"Biggest leak is <b>{_esc(worst.lower())}</b> "
        f"({_num(levers[0]['sg'],1,True)} SG/round vs scratch). "
        "Short game (greenside contact) and approach are ~75–80% of strokes over "
        "par — spend practice there, not on equipment.",
        "Greenside <b>contact</b> first: chip-save rate is "
        f"{_pct(k['chip_save_pct'])} and sand-save {_pct(k['sand_save_pct'])}. "
        "Goal is solid contact finishing inside ~15 ft, not holing it.",
        "Driving distance is the strength — keep it. Confirm any aim change on a "
        "launch monitor before trusting it on the course.",
    ]

    # ---- KPI cards ----
    kpis = [
        ("WHS Index", _num(k["index"], 1), "official, vs USGA"),
        ("GIR", _pct(k["gir_pct"], 1), "greens in regulation"),
        ("Fairways", _pct(k["fairway_pct"], 1), "off the tee"),
        ("Putts / round", _num(k["putts_per_round"], 1), "lower is better"),
        ("3-putt %", _pct(k["three_putt_pct"], 1), "lower is better"),
        ("Chip save %", _pct(k["chip_save_pct"], 1), "up & down after a chip"),
        ("Sand save %", _pct(k["sand_save_pct"], 1), "up & down from sand"),
        ("Chip error %", _pct(k["chip_error_rate"], 1), "chunked / bladed"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-v">{v}</div>'
        f'<div class="kpi-l">{lab}</div><div class="kpi-s">{sub}</div></div>'
        for lab, v, sub in kpis)

    # ---- index projection slider (client-side WHS recompute) ----
    diffs_json = json.dumps([g["diff"] for g in d["posted"]
                             if g["diff"] is not None and g.get("holes") == 18])
    official_json = json.dumps(d["player"]["index"])

    # ---- bag + suggested carry table (merges specs from bag.csv with the suggestion) ----
    specs_by = {s.get("club"): s for s in d.get("bag_specs", [])}
    bag_rows = ""
    for b in d["bag"]:
        flag = ' <span class="hold" title="held to keep the bag descending / low sample">hold</span>' if b["held"] else ""
        ms = ' <span class="conf conf-high" title="launch-monitor measured carry">measured</span>' if b.get("measured_src") else ""
        s = specs_by.get(b["club"], {})
        loft = f'{_esc(s.get("loft"))}°' if s.get("loft") else "—"
        shaft = _esc(s.get("shaft")) or "—"
        bag_rows += (
            f'<tr><td>{_esc(b["club"])}</td><td>{loft}</td>'
            f'<td class="shaft">{shaft}</td><td>{b["target"]}</td>'
            f'<td><b>{b["suggested"]}</b>{ms}{flag}</td></tr>')
    # No-carry clubs from bag.csv (e.g. the putter) — show the inventory row with a
    # dash for target/carry so the whole bag is represented, brand included.
    _bagged = {b["club"] for b in d["bag"]}
    for club, s in specs_by.items():
        if club in _bagged:
            continue
        loft = f'{_esc(s.get("loft"))}°' if s.get("loft") else "—"
        shaft = _esc(s.get("shaft")) or "—"
        bag_rows += (
            f'<tr><td>{_esc(club)}</td><td>{loft}</td>'
            f'<td class="shaft">{shaft}</td><td>—</td><td>—</td></tr>')

    # ---- dispersion table ----
    disp_rows = ""
    for c in d["dispersion"]:
        # Monte-Carlo 80% band on the total — shows how (un)certain the estimate is
        rng = (f' <span class="rng" title="Monte-Carlo 80% range from your shots">'
               f'{_num(c.get("total_lo"),0)}–{_num(c.get("total_hi"),0)}</span>'
               if c.get("total_lo") is not None else "")
        ms = ' <span class="conf conf-high" title="launch-monitor measured">measured</span>' if c.get("measured_src") else ""
        disp_rows += (
            f'<tr data-group="{_esc(c["group"])}"><td>{_esc(c["club"])}</td>'
            f'<td><b>{_num(c["total"],0)}</b>{rng}</td><td>{_num(c["carry"],0)}{ms}</td>'
            f'<td>±{_num(c["carry_sd"],0)}</td>'
            f'<td>±{_num(c["lateral_sd"],0)}</td><td>{c["n"]}</td>'
            f'<td><span class="conf conf-{_esc(c["confidence"])}">{_esc(c["confidence"])}</span></td></tr>')

    # ---- aim table ----
    if d["aim"]:
        aim_rows = "".join(
            f'<tr><td>{_esc(a["club"])}</td>'
            f'<td>{_num(abs(a["bias"]),1)} yd {a["side"]}</td>'
            f'<td>{a["n"]}</td><td>{_esc(a["rec"])}</td></tr>' for a in d["aim"])
        aim_block = (
            '<table><thead><tr><th>Club</th><th>Bias to pin line</th>'
            '<th>n</th><th>Suggestion</th></tr></thead><tbody>'
            f'{aim_rows}</tbody></table>'
            '<p class="note">Measured on <b>approach shots only</b> (aimed at green '
            'center) — tee/layup shots are excluded so a straight shot down a dogleg '
            'no longer fakes a miss. Confirm push-vs-aim on a launch monitor; never '
            'aim into a hazard.</p>')
    else:
        aim_block = ('<p class="note">Not enough geo-tagged shots yet to measure aim '
                     'bias (need ≥3 clean shots per club). Fills in as rounds post.</p>')

    # ---- approach / putting bands ----
    appr = [b for b in d["bands"].get("approach", [])
            if b["metric"] == "approach_by_pin_distance"]
    appr_rows = "".join(
        f'<tr><td>{_esc(b["slab"])} {_esc(b["unit"])}</td>'
        f'<td>{_num(b["sga"],1,True)}</td><td>{_num(b["n"],1)}</td></tr>'
        for b in appr)
    putt = [b for b in d["bands"].get("putting", [])
            if b["metric"] == "putting_by_length"]
    putt_rows = "".join(
        f'<tr><td>{_esc(b["slab"])} {_esc(b["unit"])}</td>'
        f'<td>{_num(b["sga"],1,True)}</td><td>{_num(b["n"],1)}</td></tr>'
        for b in putt)
    chip = [b for b in d["bands"].get("short", [])
            if b["metric"] == "chipping_accuracy"]
    chip_rows = "".join(
        f'<tr><td>{_esc(b["slab"])} {_esc(b["unit"])}</td>'
        f'<td>{_num(b["avg_dist"],0)} {_esc(b["dist_unit"])}</td>'
        f'<td>goal {_num(b["goal"],0)}</td></tr>' for b in chip)

    # ---- trouble holes ----
    trouble_rows = "".join(
        f'<tr><td>#{t["hole"]}</td><td>par {t["par"]}</td>'
        f'<td>{_num(t["len"],0)} yd</td><td>{_num(t["avg_to_par"],2,True)}</td>'
        f'<td>{t["n"]}</td></tr>' for t in d["trouble"])

    # ---- cost of misses ----
    lever_rows = "".join(
        f'<tr><td>{_esc(l["name"])}</td><td>{_num(l["sg"],1,True)}</td>'
        f'<td>{("recover" if l["sg"]<0 else "keep")}</td></tr>' for l in d["levers"])

    # ---- posted scores ----
    posted_rows = "".join(
        f'<tr><td>{_esc(g["date"])}</td><td>{_esc(g["course"])}</td>'
        f'<td>{g["holes"] or 18}</td><td>{g["score"]}</td><td>{_num(g["diff"],1)}'
        f'{"" if g.get("holes")==18 else " <span class=\"lc\" title=\"9-hole differential — not used in the 18-hole index until paired with another nine\">9-hole</span>"}</td>'
        f'<td>{"✓" if g["used"] else ""}</td></tr>' for g in d["posted"])

    # ---- rounds navigator (grouped by course -> round, newest first) ----
    by_course: dict[str, list] = {}
    for r in d["rounds"]:
        by_course.setdefault(r["course"] or "Unknown", []).append(r)
    nav_html = ""
    for course in sorted(by_course):
        rs = sorted(by_course[course], key=lambda r: r["date"] or "", reverse=True)
        cards = ""
        for r in rs:
            sg = _num(r["sg_total"], 1, True)
            sgcls = "pos" if (r["sg_total"] or 0) >= 0 else "neg"
            tp = r["to_par"]
            tps = f"{tp:+d}" if tp is not None else "—"
            pdf_link = (f'<a class="rc-pdf" href="rounds/{r["pdf"]}">⬇ shot-map PDF</a>'
                        if r.get("pdf") else "")
            cards += (
                f'<div class="rcard-wrap">'
                f'<a class="rcard" href="rounds/{r["slug"]}_review.html">'
                f'<div class="rc-date">{_esc(r["date"])}</div>'
                f'<div class="rc-score">{r["score"]} <span class="rc-par">({tps})</span></div>'
                f'<div class="rc-meta">{_esc(r["tee"])} · {_num(r["yards"],0)} yd · '
                f'putts {r["putts"]}</div>'
                f'<div class="rc-sg {sgcls}">SG {sg}</div>'
                f'<div class="rc-open">Open full review →</div></a>'
                f'{pdf_link}</div>')
        nav_html += (f'<div class="course-grp"><h3 class="course-h">⛳ {_esc(course)} '
                     f'<span class="course-n">{len(rs)} round'
                     f'{"s" if len(rs)!=1 else ""}</span></h3>'
                     f'<div class="rcards">{cards}</div></div>')

    # ---- SG-by-round chart + map payload ----
    sg_svg = _svg_sg_by_round(d["rounds"]) if d["rounds"] else ""
    disp_svg = _svg_dispersion(d["dispersion"]) if d["dispersion"] else ""
    map_json = json.dumps(d["map"])

    # ---- shot patterns (approach + chip, vs green center) ----
    def _tend(ls, lr):
        a = []
        if abs(ls) >= 5:
            a.append(f'{abs(ls):.0f}y {"short" if ls < 0 else "long"}')
        if abs(lr) >= 4:
            a.append(f'{abs(lr):.0f}y {"left" if lr < 0 else "right"}')
        return " · ".join(a) or "on target"

    ap = d["patterns"]["approach"]
    ap_svg = _svg_pattern(ap["points"], ap["overall"], "Approach finish vs green center",
                          ap["by_club"], "ap")
    ap_opts = "".join(f'<option value="{_cid(c["club"])}">{_esc(c["club"])}</option>'
                      for c in ap["by_club"])
    ap_rows = "".join(
        f'<tr><td>{_esc(c["club"])}</td><td>{c["n"]}</td>'
        f'<td>{abs(c["ls"]):.0f} yd {"short" if c["ls"] < 0 else "long"}</td>'
        f'<td>{abs(c["lr"]):.0f} yd {"left" if c["lr"] < 0 else "right"}</td>'
        f'<td>±{c["ls_sd"]:.0f}/{c["lr_sd"]:.0f}</td><td>{_tend(c["ls"],c["lr"])}</td></tr>'
        for c in ap["by_club"])
    sp = d["patterns"]["short"]
    chip_svg = _svg_pattern(sp["points"], sp["overall"], "Chip finish vs green center",
                            sp["by_club"], "ch")
    ch_opts = "".join(f'<option value="{_cid(c["club"])}">{_esc(c["club"])}</option>'
                      for c in sp["by_club"])
    ov = ap["overall"]
    ap_summary = (f'Across {ov["used"]} approaches (after dropping {ov["dropped"]} '
                  f'outliers) your <b>typical</b> finish (median) is '
                  f'<b>{abs(ov["ls"]):.0f} yd {"short" if ov["ls"] < 0 else "long"}</b> and '
                  f'<b>{abs(ov["lr"]):.0f} yd {"left" if ov["lr"] < 0 else "right"}</b> '
                  f'of green center.') if ov else "Not enough geo-tagged approaches yet."

    # ---- putting by distance + up & down by lie ----
    putt_rows = "".join(
        f'<tr><td>{_esc(pp["band"])}</td><td>{pp["make_pct"]}% ({pp["made"]}/{pp["att"]})'
        f'</td><td>{pp["tp"]}</td></tr>' for pp in d["putting_dist"])
    ud_rows = "".join(
        f'<tr><td>{_esc(u["lie"])}</td><td>{u["made"]}/{u["att"]}'
        f'{" ("+str(round(100*u["made"]/u["att"]))+"%)" if u["att"] else ""}</td></tr>'
        for u in d["updown"] if u["att"] is not None)

    # ---- driving accuracy (off the tee) ----
    def _fwbar(le, fw, ri):
        w = 130
        lw, fwd = le / 100 * w, fw / 100 * w
        return (f'<svg width="{w}" height="13" style="vertical-align:middle">'
                f'<rect x="0" width="{lw:.0f}" height="13" fill="var(--ink-soft)"/>'
                f'<rect x="{lw:.0f}" width="{fwd:.0f}" height="13" fill="var(--green-deep)"/>'
                f'<rect x="{lw+fwd:.0f}" width="{ri/100*w:.0f}" height="13" '
                f'fill="var(--brass)"/></svg>')
    drive_rows = "".join(
        f'<tr><td>{_esc(dd["club"])}</td><td>{dd["chances"]}</td>'
        f'<td><b>{dd["fw_pct"]}%</b></td>'
        f'<td>{_fwbar(dd["left_pct"], dd["fw_pct"], dd["right_pct"])}</td>'
        f'<td>{dd["left_pct"]}% L &nbsp; {dd["right_pct"]}% R</td></tr>'
        for dd in d["driving"])
    drv = next((x for x in d["driving"] if x["club"] == "Driver"), None)
    if drv:
        right_handed = d.get("right_handed", True)
        hook_side = "left" if right_handed else "right"
        miss_side = "left" if drv["left_pct"] >= drv["right_pct"] else "right"
        other_side = _opposite_side(miss_side)
        miss_pct = drv["left_pct"] if miss_side == "left" else drv["right_pct"]
        other_pct = drv["right_pct"] if miss_side == "left" else drv["left_pct"]
        fault = "hook" if miss_side == hook_side else "slice/push"
        drive_note = (f'Your driver finds <b>{drv["fw_pct"]}%</b> of fairways and misses '
                      f'<b>{miss_side} {miss_pct}%</b> vs {other_side} {other_pct}% — '
                      f'the {fault} shows off the tee too.')
    else:
        drive_note = ""

    # ---- pace of play ----
    def _hm(mins):
        return f'{mins // 60}h {mins % 60:02d}m'
    pace_rows = "".join(
        f'<tr><td>{_esc(p["date"])}</td><td>{_esc(p["course"])}</td><td>{p["holes"]}</td>'
        f'<td>{_hm(p["minutes"])}</td><td>{_hm(p["per18_min"])}</td></tr>'
        for p in reversed(d["pace"]))
    pace_avg = d["pace_avg18"]
    pace_txt = _hm(pace_avg) if pace_avg else "—"

    plan_html = "".join(f"<li>{x}</li>" for x in plan)
    courses = ", ".join(_esc(c) for c in m["courses"]) or "—"

    # ---- coaching: macro game plan + recent form + per-club ----
    co = d["coaching"]
    _catcol = {"Swing": "var(--ink-soft)", "Approach": "var(--green)",
               "Putting": "var(--green-deep)", "Short game": "var(--brass)",
               "Off the tee": "var(--ink)", "Course mgmt": "var(--brass)"}
    top10_rows = "".join(
        f'<li><span class="t10-rank">{t["rank"]}</span>'
        f'<span class="sgpill">+{t["sg"]}</span>'
        f'<div class="t10-body"><div class="t10-head">{_esc(t["action"])}'
        f'<span class="t10-cat" style="color:{_catcol.get(t["cat"], "var(--ink-soft)")}">'
        f'{_esc(t["cat"])}</span></div>'
        f'<div class="note">{t["detail"]}</div></div></li>' for t in co["top10"])
    top10_section = f"""
<section data-tab="overall"><h2>Top 10 &mdash; what to do, by strokes gained</h2>
<ol class="top10">{top10_rows}</ol>
<p class="note">Each <b style="color:var(--good)">+x.x</b> is the estimated strokes/round
you'd gain by closing that gap toward a mid-handicap baseline &mdash; scaled from your own
category losses (approach, putting, off-tee, short game). They overlap, so the realistic
combined gain is about <b>{d['recoverable']['effective']}/round</b>, not the
{co['top10_total']} straight sum. Work top-down: #1 is the highest-leverage thing you can
do.</p></section>"""
    # ---- Recent form: per-category trend cards (▲ improving / ▼ slipping) ----
    def _trend(dlt):
        if dlt is None:
            return ("flat", "–")
        if dlt >= 0.05:
            return ("up", "▲")
        if dlt <= -0.05:
            return ("down", "▼")
        return ("flat", "–")
    form_cards = ""
    for c in co["sg_compare"]:
        cls, arr = _trend(c["delta"])
        dtxt = _num(c["delta"], 1, True) if c["delta"] is not None else "—"
        form_cards += (
            f'<div class="fcard {cls}"><div class="fcard-cat">{_esc(c["cat"])}</div>'
            f'<div class="fcard-delta {cls}">{arr}&nbsp;{dtxt}</div>'
            f'<div class="fcard-sub"><b>{_num(c["recent"], 1, True)}</b> now'
            f'<span class="fcard-base"> · {_num(c["macro"], 1, True)} base</span></div></div>')
    _ups = [c["cat"].lower() for c in co["sg_compare"] if (c["delta"] or 0) >= 0.05]
    _downs = [c["cat"].lower() for c in co["sg_compare"] if (c["delta"] or 0) <= -0.05]
    if _downs and len(_ups) >= 2:
        form_hl = (f"Trending up across the board &mdash; {', '.join(_downs)} "
                   f"{'is' if len(_downs) == 1 else 'are'} the one cold spot. Warm "
                   f"that up before the next round.")
    elif _downs:
        form_hl = f"Watch your {', '.join(_downs)} &mdash; slipping vs your baseline."
    elif _ups:
        form_hl = "Everything's trending up vs your baseline. Keep it rolling."
    else:
        form_hl = "Holding steady vs your baseline."
    flags_html = "".join(
        f'<li><b>{it["head"]}</b> &mdash; <span class="note">{it["body"]}</span></li>'
        for it in co["recent"])
    club_rec_rows = "".join(
        f'<tr><td>{_esc(c["club"])}</td><td>{c["rec"]}</td>'
        f'<td class="note">{_esc(c["basis"])}</td></tr>' for c in co["by_club"])
    coach_overall = f"""
<section data-tab="overall"><h2>Recent form {('&mdash; ' + co['recent_label']) if co['recent_label'] else ''}</h2>
<div class="formgrid">{form_cards or '<p class="note">Not enough rounds yet.</p>'}</div>
<p class="form-hl">{form_hl}</p>
{('<ul class="flags">' + flags_html + '</ul>') if flags_html else ''}
<p class="note"><b>▲ / ▼</b> = your last 2 rounds vs your all-rounds baseline (strokes
gained per round; less negative is better). It's the warm-up signal &mdash; what's hot or
cold <i>right now</i> &mdash; while the Top&nbsp;10 above is the all-rounds plan.</p></section>"""

    coach_byclub = f"""
<section data-tab="club"><h2>By club &mdash; what to try</h2>
<table><thead><tr><th>Club</th><th>Try this</th><th>basis</th></tr></thead>
<tbody>{club_rec_rows}</tbody></table>
<div class="key"><b>Reading this:</b>
<ul>
<li><b>Club up</b> = use the next longer club (one lower number / less loft &mdash; e.g.
6-iron instead of 7-iron). You're landing short, so you need more club.</li>
<li><b>Club down</b> = next shorter club; you're flying it past the target.</li>
<li><b>Aim ~Xy right / left</b> = your typical finish is that many yards to one side, so
start your aim the other way to bring it back to the middle. <i>This is a patch</i> —
if most clubs lean the same way it's a swing fault (that's #1 in the Top 10); fixing the
face/path squares them all up at once.</li>
<li><b>Distance dialed / on line</b> = no change &mdash; keep doing it.</li>
</ul>
<span class="note">Structural tendencies from your median (outlier-cleaned) shots; 4
rounds in, treat low-sample clubs as directional. "basis" = how many shots it's
from.</span></div></section>"""

    # ---- By-club tab: bag (top) -> merged stats -> what to try (bottom) ----
    club_bag = f"""
<section data-tab="club"><h2>Your bag &amp; distances</h2>
<table><thead><tr><th>Club</th><th>Loft</th><th>Shaft</th><th>Target</th>
<th>Carry to use</th></tr></thead><tbody>{bag_rows}</tbody></table>
<p class="note">Your bag (from <code>bag.csv</code>) with the <b>"Carry to use"</b> &mdash;
the single number to punch into your apps: your measured carry when there's enough of it,
else your target, always kept <b>strictly descending</b> ("hold" = a noisy point would
have broken club order; "measured" = a launch-monitor number).</p>
<h3 class="sub">Measured distances &amp; dispersion</h3>
{disp_svg}
<div class="tabs" id="disp-tabs" style="margin:12px 0 8px">
 <button class="on" data-g="all">All</button><button data-g="Woods">Woods</button>
 <button data-g="Irons">Irons</button><button data-g="Wedges">Wedges</button></div>
<table><thead><tr><th>Club</th><th>Total (best ⅓)</th><th>Carry</th><th>Carry ±SD</th>
<th>Lateral ±SD</th><th>Shots</th><th>Confidence</th></tr></thead>
<tbody id="disp-body">{disp_rows}</tbody></table>
<p class="note"><b>Total</b> = real measured distance (carry + roll) from Arccos &mdash;
best-third strike, recency-weighted, Monte-Carlo bootstrapped (grey range = 80% band).
<b>Carry</b> = total × a condition-aware roll factor (you play wet, so carry sits just
under total). Modeled until <i>launch-monitor carries (Tee Box, July)</i> take over.</p></section>"""

    club_shots = f"""
<section data-tab="club"><h2>Where your shots go</h2>
<h3 class="sub">Off the tee &mdash; driving accuracy</h3>
<table><thead><tr><th>Tee club</th><th>Tee shots</th><th>Fairways</th>
<th>L &nbsp;|&nbsp; fairway &nbsp;|&nbsp; R</th><th>Miss split</th></tr></thead>
<tbody>{drive_rows or '<tr><td colspan=5>—</td></tr>'}</tbody></table>
<p class="note">Direction off the tee from Arccos's fairway hit + miss side (you aim down
the fairway, not the green). {drive_note}</p>
<h3 class="sub">Approaches &amp; chips &mdash; finish vs green center</h3>
<p class="note">{ap_summary} Target = <b>green center</b> (you aim at the middle, not the
flag). Up = long, down = short. Outliers dropped via IQR; the white ring is your
<b>median miss</b>.</p>
<div style="display:flex;flex-wrap:wrap;gap:18px;justify-content:center">
<div><div style="text-align:center;margin-bottom:6px"><span class="note">Approaches —
filter by club: </span><select class="clubsel" onchange="filterPat('ap',this.value)">
<option value="all">All clubs</option>{ap_opts}</select></div>{ap_svg}</div>
<div><div style="text-align:center;margin-bottom:6px"><span class="note">Chips —
filter by club: </span><select class="clubsel" onchange="filterPat('ch',this.value)">
<option value="all">All clubs</option>{ch_opts}</select></div>{chip_svg}</div>
</div>
<h3 class="sub">Approach miss by club</h3>
<table><thead><tr><th>Club</th><th>n</th><th>Short / long</th><th>Left / right</th>
<th>±SD (l-s/l-r)</th><th>Tendency</th></tr></thead><tbody>{ap_rows or '<tr><td colspan=6>—</td></tr>'}</tbody></table>
<p class="note">Distance-control reality: the <b>median</b> short/long (mishits excluded)
is what your club <i>typically</i> does &mdash; long irons short = take more club.</p>
<h3 class="sub">Aim by club</h3>
{aim_block}</section>"""

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strokes-Gained Tracker — {html.escape(PLAYER_NAME)}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
@font-face{{font-family:'Besley';src:url('{font_prefix}/Besley-var.woff2') format('woff2');font-weight:600 700;font-style:normal;font-display:swap}}
@font-face{{font-family:'Besley';src:url('{font_prefix}/Besley-italic.woff2') format('woff2');font-weight:600;font-style:italic;font-display:swap}}
@font-face{{font-family:'Atkinson Hyperlegible';src:url('{font_prefix}/AtkinsonHyperlegible-400.woff2') format('woff2');font-weight:400;font-style:normal;font-display:swap}}
@font-face{{font-family:'Atkinson Hyperlegible';src:url('{font_prefix}/AtkinsonHyperlegible-400i.woff2') format('woff2');font-weight:400;font-style:italic;font-display:swap}}
@font-face{{font-family:'Atkinson Hyperlegible';src:url('{font_prefix}/AtkinsonHyperlegible-700.woff2') format('woff2');font-weight:700;font-style:normal;font-display:swap}}
:root{{--paper:oklch(0.965 0.012 95);--paper-deep:oklch(0.935 0.018 95);--ink:oklch(0.28 0.045 155);
--ink-soft:oklch(0.46 0.03 155);--green:oklch(0.42 0.09 155);--green-deep:oklch(0.33 0.08 155);
--brass:oklch(0.58 0.10 85);--brass-soft:oklch(0.88 0.04 90);--rule:oklch(0.84 0.022 100);
--serif:'Besley',Georgia,serif;--sans:'Atkinson Hyperlegible',system-ui,sans-serif;
--sp-xs:4px;--sp-sm:8px;--sp-md:16px;--sp-lg:24px;--sp-xl:48px;--sp-2xl:96px;
--mut:var(--ink-soft);--line:var(--rule);--good:var(--green-deep);--bad:oklch(0.45 0.13 30);--accent:var(--green)}}
*{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%;background:var(--paper)}}
body{{margin:0;background:var(--paper);color:var(--ink);
font:400 15px/1.55 var(--sans);font-variant-numeric:tabular-nums}}
header{{padding:28px 20px 0;max-width:1000px;margin:0 auto}}
header::after{{content:"";display:block;border-top:1px solid var(--ink);border-bottom:1px solid var(--ink);
height:3px;margin-top:var(--sp-md)}}
h1{{font:600 1.95rem/1.15 var(--serif);letter-spacing:0;margin:0 0 var(--sp-xs);color:var(--ink)}}
h2{{font:700 .8rem/1 var(--sans);letter-spacing:.11em;text-transform:uppercase;
color:var(--brass);margin:0 0 var(--sp-sm)}}
h2::after{{content:"";display:block;height:1px;background:var(--rule);margin-top:var(--sp-sm)}}
.sub{{color:var(--ink-soft);font-size:.86rem}}
main{{max-width:1000px;margin:0 auto;padding:8px 20px 72px}}
section{{background:transparent;border:0;border-top:1px solid var(--rule);border-radius:0;
padding:18px 0 0;margin:34px 0 0}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
@media(max-width:640px){{.kpis{{grid-template-columns:repeat(2,1fr)}}}}
.kpi{{background:var(--paper);border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);
border-radius:0;padding:12px 2px 13px}}
.kpi-v{{font:600 1.55rem/1.05 var(--serif);color:var(--green-deep);font-variant-numeric:tabular-nums}}
.kpi-l{{font:700 .72rem/1.1 var(--sans);letter-spacing:.08em;text-transform:uppercase;
color:var(--ink);margin-top:6px}}
.kpi-s{{font-size:.76rem;color:var(--ink-soft);margin-top:3px}}
table{{width:100%;border-collapse:collapse;font-size:.9rem;margin:var(--sp-md) 0;
font-variant-numeric:tabular-nums}}
thead th{{font:700 .72rem/1 var(--sans);letter-spacing:.09em;text-transform:uppercase;
color:var(--ink-soft);border-top:3px double var(--ink);border-bottom:1px solid var(--ink);
padding:10px 10px 8px;text-align:right}}
td{{text-align:right;padding:9px 10px;border-bottom:1px solid var(--rule);vertical-align:top}}
th:first-child,td:first-child{{text-align:left;padding-left:2px}}
th:last-child,td:last-child{{padding-right:2px}}
tbody tr:hover{{background:var(--paper-deep)}}
td b{{color:var(--green-deep)}}
.note{{color:var(--ink-soft);font-size:.82rem;margin:10px 0 0}}
h3.sub{{font:700 .74rem/1 var(--sans);letter-spacing:.11em;text-transform:uppercase;
color:var(--brass);margin:24px 0 8px}}
h3.sub .note{{display:inline;font-weight:400}}
a{{color:var(--green);text-decoration-color:color-mix(in oklch,var(--green) 40%,transparent)}}
a:hover{{color:var(--green-deep)}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.95em;color:var(--green-deep)}}
ol.top10{{list-style:none;margin:0;padding:0}}
ol.top10 li{{display:flex;gap:12px;align-items:flex-start;padding:11px 2px;
border-bottom:1px solid var(--rule)}}
ol.top10 li:last-child{{border-bottom:none}}
.t10-rank{{flex:0 0 18px;color:var(--ink-soft);font-weight:700;font-size:15px;
text-align:right;padding-top:3px}}
.sgpill{{flex:0 0 auto;min-width:54px;text-align:center;background:var(--brass-soft);
color:var(--green-deep);border:0;border-radius:2px;padding:4px 8px;
font-weight:700;font-size:15px;font-variant-numeric:tabular-nums}}
.t10-body{{flex:1 1 auto;min-width:0}}
.t10-head{{font-weight:600;font-size:14.5px;line-height:1.35}}
.t10-cat{{font-size:10px;text-transform:uppercase;letter-spacing:.04em;font-weight:700;
margin-left:8px;white-space:nowrap}}
.t10-body .note{{margin:3px 0 0}}
.tabbar{{position:sticky;top:0;z-index:50;display:flex;gap:6px;flex-wrap:wrap;
background:var(--paper);padding:12px 0 10px;margin:0 0 4px;border-bottom:1px solid var(--rule)}}
.tabbar .tab{{flex:1 1 auto;min-width:120px;background:transparent;color:var(--ink-soft);
border:0;border-bottom:2px solid transparent;border-radius:0;padding:10px 14px;font-size:14px;
font-weight:700;letter-spacing:.04em;text-transform:uppercase;cursor:pointer}}
.tabbar .tab:hover{{color:var(--green-deep)}}
.tabbar .tab.on{{background:transparent;color:var(--ink);border-bottom-color:var(--brass)}}
section[data-tab]{{display:none}}
.key{{background:var(--paper-deep);border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);
border-radius:0;padding:10px 2px;margin-top:12px;font-size:.82rem;color:var(--ink-soft)}}
.key ul{{margin:6px 0 4px;padding-left:18px}} .key li{{margin:3px 0}}
.key b{{color:var(--ink)}}
.recs{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
@media(max-width:640px){{.recs,.grid2{{grid-template-columns:1fr}}
main{{padding:8px 12px 60px}} section{{padding-top:14px}}
th,td{{padding:6px 5px;font-size:12.5px}}
.tabbar .tab{{font-size:12.5px;padding:9px 6px;min-width:0;flex:1 1 0}}
.t10-head{{font-size:13.5px}} .sgpill{{min-width:46px;font-size:14px}}
table{{display:block;overflow-x:auto;white-space:nowrap}}
table.sgc{{white-space:normal}}}}
.rec{{background:transparent;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);
border-radius:0;padding:11px 2px}}
.rec-tag{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--brass);
font-weight:700}}
.rec-head{{font-weight:600;margin:3px 0 4px;font-size:14px}}
.rec-body{{color:var(--ink-soft);font-size:12.5px;line-height:1.45}}
.grid2{{display:grid;grid-template-columns:1.1fr 1fr;gap:14px;align-items:start}}
.formgrid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
@media(max-width:640px){{.formgrid{{grid-template-columns:repeat(2,1fr)}}}}
.fcard{{background:transparent;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);
border-radius:0;padding:12px 2px}}
.fcard.up{{border-top-color:var(--green-deep)}} .fcard.down{{border-top-color:var(--bad)}}
.fcard-cat{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;
color:var(--brass);font-weight:700}}
.fcard-delta{{font:600 1.45rem/1.05 var(--serif);margin:5px 0 3px;color:var(--ink-soft);
font-variant-numeric:tabular-nums}}
.fcard-delta.up{{color:var(--green-deep)}} .fcard-delta.down{{color:var(--bad)}}
.fcard-sub{{font-size:12.5px;color:var(--ink-soft)}} .fcard-sub b{{color:var(--ink);
font-size:14px}} .fcard-base{{white-space:nowrap}}
.form-hl{{font-size:14px;font-weight:600;margin:14px 0 0}}
ul.flags{{margin:10px 0 0;padding-left:18px}} ul.flags li{{margin:0 0 6px}}
table.sgc{{max-width:520px}}
ul.recent{{margin:12px 0 0;padding-left:18px}} ul.recent li{{margin:0 0 9px}}
table.sgc th:not(:first-child),table.sgc td:not(:first-child){{text-align:right;
font-variant-numeric:tabular-nums;width:84px}}
table.sgc td.pos{{color:var(--green-deep)}} table.sgc td.neg{{color:var(--bad)}}
td.pos{{color:var(--green-deep)}} td.neg{{color:var(--bad)}}
.hold,.lc{{font-size:10px;padding:1px 5px;border-radius:2px;background:var(--brass-soft);color:var(--ink)}}
.lc{{background:color-mix(in oklch,var(--bad) 13%,var(--paper));color:var(--bad)}}
.rng{{font-size:10px;color:var(--ink-soft)}}
.shaft{{font-size:11px;color:var(--ink-soft)}}
.conf{{font-size:11px;padding:1px 7px;border-radius:2px}}
.conf-high{{background:color-mix(in oklch,var(--green) 13%,var(--paper));color:var(--green-deep)}}
.conf-medium{{background:var(--brass-soft);color:var(--ink)}}
.conf-low{{background:color-mix(in oklch,var(--bad) 13%,var(--paper));color:var(--bad)}}
ul{{margin:0;padding-left:18px}} li{{margin:6px 0}}
img.chart{{width:100%;height:auto;border-radius:0;background:var(--paper);padding:6px}}
.tabs button{{background:transparent;color:var(--ink-soft);border:0;border-bottom:2px solid transparent;
border-radius:0;padding:6px 10px;margin-right:6px;cursor:pointer;font-weight:700;
letter-spacing:.04em;text-transform:uppercase}}
.tabs button:hover{{color:var(--green-deep)}}
.tabs button.on{{background:transparent;color:var(--ink);border-bottom-color:var(--brass)}}
#map{{height:440px;border:1px solid var(--rule);border-radius:2px;margin-top:10px;background:var(--paper-deep)}}
.slider-row{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:10px 0}}
input[type=range]{{flex:1;min-width:180px}}
.proj{{font:600 1.55rem/1 var(--serif);color:var(--green-deep);font-variant-numeric:tabular-nums}}
select{{background:oklch(0.985 0.008 95);border:1px solid var(--rule);border-radius:2px;
color:var(--ink);font:400 .95rem/1.2 var(--sans);padding:6px 8px}}
.pos{{color:var(--green-deep)}} .neg{{color:var(--bad)}}
.modeled{{border-bottom:1px dotted var(--ink-soft);cursor:help}}
.course-grp{{margin:14px 0}}
.course-h{{font:600 1.05rem/1.2 var(--serif);margin:0 0 8px}}
.course-n{{color:var(--ink-soft);font:400 .78rem/1 var(--sans)}}
.rcards{{display:block;border-top:3px double var(--ink)}}
.rcard-wrap{{display:block;border-bottom:1px solid var(--rule)}}
.rcard{{display:grid;grid-template-columns:minmax(7rem,.8fr) minmax(5rem,.45fr) minmax(13rem,1.2fr) minmax(4rem,.35fr) auto;
gap:var(--sp-md);align-items:baseline;text-decoration:none;color:var(--ink);background:transparent;
border:0;border-radius:0;padding:14px 2px}}
.rcard:hover{{background:var(--paper-deep)}}
.rc-pdf{{font-size:11.5px;color:var(--ink-soft);text-decoration:none;padding:4px 2px 10px;display:inline-block}}
.rc-pdf:hover{{color:var(--green-deep)}}
.rc-date{{font-size:12px;color:var(--ink-soft)}}
.rc-score{{font:600 1.55rem/1 var(--serif);margin:0;color:var(--green-deep);font-variant-numeric:tabular-nums}}
.rc-par{{font:.88rem/1 var(--sans);color:var(--ink-soft);font-weight:400}}
.rc-meta{{font-size:11.5px;color:var(--ink-soft)}}
.rc-sg{{font-size:13px;font-weight:600;margin-top:6px}}
.rc-open{{font-size:12px;color:var(--green);margin-top:6px}}
footer{{max-width:1000px;margin:0 auto;padding:20px 20px 50px;color:var(--ink-soft);
font-size:12px;border-top:1px solid var(--rule)}}
@media(max-width:760px){{.rcard{{grid-template-columns:1fr auto;gap:6px 12px}}
.rc-meta,.rc-sg,.rc-open{{grid-column:1/-1}}}}
</style></head>
<body>
<header>
<h1>⛳ Strokes-Gained Tracker</h1>
<div class="sub">{html.escape(PLAYER_NAME)} · {courses} · {m['n_rounds']} rounds · {m['n_holes']} holes ·
{m['n_shots']} shots · index <b>{_num(p['index'],1)}</b> ·
generated {_esc(m['generated_at'])}</div>
</header>
<main>

<nav class="tabbar" role="tablist">
 <button class="tab on" data-pane="overall">Overall &mdash; game &amp; handicap</button>
 <button class="tab" data-pane="club">By club</button>
 <button class="tab" data-pane="round">By round</button>
</nav>

{top10_section}

<section data-tab="overall"><h2>Key numbers</h2><div class="kpis">{kpi_html}</div>
<p class="note">{('Targets for ' + html.escape(GOLF_EVENT) + '. ') if GOLF_EVENT else ''}All strokes-gained is
Arccos, measured vs scratch — large negatives are normal for a mid-handicap.</p></section>
{coach_overall}
{club_bag}
{club_shots}
{coach_byclub}

<section data-tab="round"><h2>Rounds — full reviews</h2>
<p class="note">Click any round to open its full review: satellite shot map,
hole-by-hole, and strokes-gained for that round.</p>
{nav_html or '<p class="note">No rounds yet.</p>'}</section>

<section data-tab="overall"><h2>Strokes gained by round</h2>
{sg_svg or '<p class="note">No rounds yet.</p>'}
</section>

<section data-tab="overall"><h2>Cost of misses</h2>
<table><thead><tr><th>Category</th><th>SG / round</th><th>Lever</th></tr></thead>
<tbody>{lever_rows}</tbody></table>
<p class="note">Recoverable if you stopped bleeding strokes in the negative
categories: <b>{d['recoverable']['raw']}</b> raw. SG levers overlap ~35–40%, so the
realistic combined gain is about <b>{d['recoverable']['effective']}</b> strokes/round
(0.62 efficiency factor — don't add them straight up).</p></section>

<section data-tab="overall"><h2>Approach & putting</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:18px" class="twocol">
 <div><h3 style="font-size:14px;color:var(--mut)">Approach SG by pin distance</h3>
 <table><thead><tr><th>Band</th><th>SG</th><th>n</th></tr></thead>
 <tbody>{appr_rows or '<tr><td colspan=3>—</td></tr>'}</tbody></table></div>
 <div><h3 style="font-size:14px;color:var(--mut)">Putting SG by length</h3>
 <table><thead><tr><th>Length</th><th>SG</th><th>n</th></tr></thead>
 <tbody>{putt_rows or '<tr><td colspan=3>—</td></tr>'}</tbody></table></div>
</div></section>

<section data-tab="overall"><h2>Scrambling — the priority</h2>
<table><thead><tr><th>Chip distance</th><th>Avg proximity</th><th>Target</th></tr></thead>
<tbody>{chip_rows or '<tr><td colspan=3>—</td></tr>'}</tbody></table>
<p class="note">Chip save {_pct(k['chip_save_pct'])} · sand save
{_pct(k['sand_save_pct'])} · chip error {_pct(k['chip_error_rate'])}. The miss is
<b>contact</b>, not the putt that follows — proximity is measured from where the chip
finishes. Fix the chunk first.</p></section>

<section data-tab="overall"><h2>Putting &amp; up-and-down</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:18px" class="twocol">
 <div><h3 style="font-size:14px;color:var(--mut)">One-putt % by first-putt distance</h3>
 <table><thead><tr><th>Distance</th><th>One-putt</th><th>3-putts</th></tr></thead>
 <tbody>{putt_rows or '<tr><td colspan=3>—</td></tr>'}</tbody></table></div>
 <div><h3 style="font-size:14px;color:var(--mut)">Up &amp; down by lie</h3>
 <table><thead><tr><th>From</th><th>Saved</th></tr></thead>
 <tbody>{ud_rows or '<tr><td colspan=2>—</td></tr>'}</tbody></table></div>
</div>
<p class="note">Where your 3-putts come from (almost always long range → it's a lag /
approach-proximity problem, not a stroke problem) and your greenside save rate by lie.</p></section>

<section data-tab="round"><h2>Round map</h2>
<div class="tabs" id="round-tabs"></div>
<div id="map"></div>
<p class="note">Satellite tiles (Esri) load in the browser only. Each line is a hole's
shot path from GPS. No GPS on a round → it won't appear here.</p></section>

<section data-tab="round"><h2>Trouble holes</h2>
<table><thead><tr><th>Hole</th><th>Par</th><th>Length</th><th>Avg vs par</th>
<th>n</th></tr></thead><tbody>{trouble_rows}</tbody></table>
<p class="note">Needs ~5+ rounds before this is signal rather than noise.</p></section>

<section data-tab="round"><h2>Pace of play</h2>
<table><thead><tr><th>Date</th><th>Course</th><th>Holes</th><th>Round time</th>
<th>Pace / 18</th></tr></thead><tbody>{pace_rows or '<tr><td colspan=5>—</td></tr>'}</tbody></table>
<p class="note">Your rounds average <b>{pace_txt} per 18 holes</b> (from Arccos round
timestamps). For reference: a brisk pace is ~4h00m, ~4h30m is slow, and <b>5h+ is a
grind</b>. Empirical backing for the pace-of-play conversation{(' at ' + html.escape(GOLF_HOME_COURSE)) if GOLF_HOME_COURSE else ''}.</p></section>

<section data-tab="overall"><h2>Index projection</h2>
<p class="note">Your official index is <b>{_num(p['index'],1)}</b> (from GHIN). This is a
rough <i>estimate</i> of where it heads — WHS counts your most recent 20 differentials,
and with few scores a small-sample adjustment shrinks as you post, so the index can
drift before settling. It can differ from GHIN by up to a stroke until ~20 scores are
in; GHIN's number above is the truth.</p>
<div class="slider-row">
  <label>If your next rounds average a differential of
  <b id="dval">18</b>:</label>
  <input id="dslider" type="range" min="6" max="30" value="18" step="0.5">
</div>
<div class="slider-row">
  <span>After
  <select id="nposts">
   <option>3</option><option>5</option><option selected>8</option>
   <option>12</option><option>20</option></select> more posted scores →
  projected index <span class="proj" id="projidx">—</span></span>
</div></section>

<section data-tab="overall"><h2>Posted scores (GHIN)</h2>
<table><thead><tr><th>Date</th><th>Course</th><th>Holes</th><th>Score</th>
<th>Differential</th><th>Counts</th></tr></thead><tbody>{posted_rows}</tbody></table>
<p class="note">Only 18-hole differentials feed the WHS index. A 9-hole round posts a
9-hole differential that's held until it pairs with another nine — so it's shown here
but flagged, not counted in your index yet.</p></section>

</main>
<footer>
Generated by <code>dashboard/gen_tracker.py</code> from the arccos-sg-baseline data
store. Strokes-gained categories are Arccos (modeled vs scratch); per-shot SG is a
Broadie reconstruction; make%/proximity are derived; peer/CHS/age figures are modeled
estimates. Equipment is low-leverage (~2 strokes) — short game and course management
are the lever.
</footer>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// ---- WHS index projection (client-side recompute) ----
var DIFFS = {diffs_json};
var OFFICIAL = {official_json};   // GHIN's authoritative current index
var WHS = {{3:[1,2],4:[1,1],5:[1,0],6:[2,1],7:[2,0],8:[2,0],9:[3,0],10:[3,0],
11:[3,0],12:[4,0],13:[4,0],14:[4,0],15:[5,0],16:[5,0],17:[6,0],18:[6,0],19:[7,0],20:[8,0]}};
function whs(arr){{var a=arr.slice().filter(x=>x!=null).sort((p,q)=>p-q);
var n=a.length; if(n<3) return null; var t=WHS[Math.min(n,20)]||[8,0];
var s=0; for(var i=0;i<t[0];i++) s+=a[i]; return Math.round((s/t[0]-t[1])*10)/10;}}
// Estimate only. WHS uses your most recent 20 differentials; we can't perfectly
// mirror GHIN's small-sample adjustment / 9-hole pairing, so this is a rough
// trajectory, not the official number (which is shown above).
function proj(){{
 var dv=parseFloat(document.getElementById('dslider').value);
 var np=parseInt(document.getElementById('nposts').value);
 document.getElementById('dval').textContent=dv;
 var arr=DIFFS.slice(); for(var i=0;i<np;i++) arr.push(dv);
 arr = arr.slice(-20);   // WHS counts only the most recent 20 scores
 var v=whs(arr);
 document.getElementById('projidx').textContent = v==null?'—':'~'+v.toFixed(1);
}}
document.getElementById('dslider').addEventListener('input',proj);
document.getElementById('nposts').addEventListener('change',proj);
proj();

// ---- dispersion group filter ----
document.querySelectorAll('#disp-tabs button').forEach(function(b){{
 b.onclick=function(){{
  document.querySelectorAll('#disp-tabs button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); var g=b.dataset.g;
  document.querySelectorAll('#disp-body tr').forEach(function(tr){{
   tr.style.display=(g==='all'||tr.dataset.group===g)?'':'none';}});
 }};}});

// ---- shot-pattern per-club filter (pid = 'ap' approach / 'ch' chips) ----
function filterPat(pid, v){{
 document.querySelectorAll('.'+pid+'-dot').forEach(function(e){{
  e.style.display=(v==='all'||e.classList.contains(pid+'-dot-'+v))?'':'none';}});
 document.querySelectorAll('.'+pid+'-avg').forEach(function(e){{e.style.display='none';}});
 var a=document.querySelector('.'+pid+'-avg-'+v); if(a) a.style.display='';
}}

// ---- round map (Esri satellite) ----
var ROUNDS={map_json};
var MAP_GREEN='oklch(0.42 0.09 155)';
var MAP_BRASS='oklch(0.58 0.10 85)';
var map=L.map('map',{{scrollWheelZoom:false}});
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
 {{maxZoom:19,attribution:'Esri'}}).addTo(map);
var layer=L.layerGroup().addTo(map);
function drawRound(r){{
 layer.clearLayers(); var all=[];
 (r.holes||[]).forEach(function(h){{
 if(h.pts.length<2) return;
  var pl=L.polyline(h.pts,{{color:MAP_BRASS,weight:2,opacity:.9}}).addTo(layer);
  h.pts.forEach(p=>all.push(p));
  L.circleMarker(h.pts[0],{{radius:3,color:MAP_GREEN,fillColor:MAP_GREEN,fillOpacity:1}})
    .bindTooltip('Hole '+h.hole).addTo(layer);
 }});
 if(all.length) map.fitBounds(all,{{padding:[20,20]}});
}}
var tabs=document.getElementById('round-tabs');
if(ROUNDS.length){{
 ROUNDS.forEach(function(r,i){{
  var b=document.createElement('button');
  b.textContent=(r.date||r.round_id)+' · '+(r.course||'');
  b.className=i===ROUNDS.length-1?'on':'';
  b.onclick=function(){{tabs.querySelectorAll('button').forEach(x=>x.classList.remove('on'));
   b.classList.add('on'); drawRound(r);}};
  tabs.appendChild(b);
 }});
 drawRound(ROUNDS[ROUNDS.length-1]);
}}else{{
 document.getElementById('map').innerHTML='<p class="note" style="padding:20px">No GPS rounds to map yet.</p>';
}}

// ---- top-level tabs (Overall / By club / By round) ----
function showPane(pane){{
 document.querySelectorAll('section[data-tab]').forEach(function(s){{
  s.style.display=(s.dataset.tab===pane)?'block':'none';}});
 document.querySelectorAll('.tabbar .tab').forEach(function(b){{
  b.classList.toggle('on', b.dataset.pane===pane);}});
 if(pane==='round' && typeof map!=='undefined'){{
  setTimeout(function(){{map.invalidateSize();
   var r=ROUNDS&&ROUNDS.length?ROUNDS[ROUNDS.length-1]:null; if(r) drawRound(r);}},60);}}
 if(history.replaceState) history.replaceState(null,'','#'+pane);
}}
document.querySelectorAll('.tabbar .tab').forEach(function(b){{
 b.onclick=function(){{showPane(b.dataset.pane);
  window.scrollTo({{top:0,behavior:'smooth'}});}};}});
var _p=(location.hash||'').replace('#','');
showPane(['overall','club','round'].indexOf(_p)>=0?_p:'overall');
// keep tabs in sync with the hash (back/forward buttons, hash links)
window.addEventListener('hashchange',function(){{
 var p=(location.hash||'').replace('#','');
 if(['overall','club','round'].indexOf(p)>=0) showPane(p);
}});
</script>
</body></html>"""


def render_round_page(d: dict, r: dict, font_prefix: str = "../assets/fonts") -> str:
    """Full review for one round: satellite shot map + hole-by-hole + SG."""
    font_prefix = font_prefix.rstrip("/")
    holes = d["holes_by_round"].get(r["round_id"], [])
    shotmap_json = json.dumps(d["shotmap"].get(r["round_id"], []))
    tp = r["to_par"]
    tps = f"{tp:+d}" if tp is not None else "—"

    kpi = [
        ("Score", f'{r["score"]} <span class="rc-par">({tps})</span>'),
        ("Putts", _num(r["putts"], 0)),
        ("GIR", _pct(r["gir"], 0)), ("Fairways", _pct(r["fairway"], 0)),
        ("Scramble", _pct(r["scramble"], 0)),
        ("SG total", _num(r["sg_total"], 1, True)),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-v">{v}</div><div class="kpi-l">{lab}</div></div>'
        for lab, v in kpi)
    sg_html = "".join(
        f'<div class="kpi"><div class="kpi-v">{_num(r[k],1,True)}</div>'
        f'<div class="kpi-l">{lab}</div></div>'
        for lab, k in [("Off the tee", "sg_off_tee"), ("Approach", "sg_approach"),
                       ("Short game", "sg_short"), ("Putting", "sg_putting")])

    def _tp(v):
        return "" if v is None else (f"+{v}" if v > 0 else str(v))
    hrows = "".join(
        f'<tr><td>#{h["hole"]}</td><td>{h["par"]}</td><td>{_num(h["len"],0)}</td>'
        f'<td>{h["shots"]}</td><td>{_tp(h["to_par"])}</td>'
        f'<td>{h["putts"]}</td><td>{"✓" if h["fairway"] else ""}</td>'
        f'<td>{"✓" if h["gir"] else ""}</td>'
        f'<td class="teeclub">{_esc(h.get("drive_club") or "")}</td>'
        f'<td>{_num(h["drive"],0)}</td>'
        f'<td>{_num(h["proximity"],0)}</td><td>{_num(h["sg"],2,True)}</td></tr>'
        for h in holes)

    weather = ""
    if r.get("temp_f") or r.get("weather"):
        weather = (f' · {_num(r["temp_f"],0)}°F'
                   f'{" · wind "+_num(r["wind_mph"],0)+" mph" if r.get("wind_mph") else ""}'
                   f'{" · "+_esc(r["weather"]) if r.get("weather") else ""}')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(r['course'])} · {_esc(r['date'])} — round review</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
@font-face{{font-family:'Besley';src:url('{font_prefix}/Besley-var.woff2') format('woff2');font-weight:600 700;font-style:normal;font-display:swap}}
@font-face{{font-family:'Besley';src:url('{font_prefix}/Besley-italic.woff2') format('woff2');font-weight:600;font-style:italic;font-display:swap}}
@font-face{{font-family:'Atkinson Hyperlegible';src:url('{font_prefix}/AtkinsonHyperlegible-400.woff2') format('woff2');font-weight:400;font-style:normal;font-display:swap}}
@font-face{{font-family:'Atkinson Hyperlegible';src:url('{font_prefix}/AtkinsonHyperlegible-400i.woff2') format('woff2');font-weight:400;font-style:italic;font-display:swap}}
@font-face{{font-family:'Atkinson Hyperlegible';src:url('{font_prefix}/AtkinsonHyperlegible-700.woff2') format('woff2');font-weight:700;font-style:normal;font-display:swap}}
:root{{--paper:oklch(0.965 0.012 95);--paper-deep:oklch(0.935 0.018 95);--ink:oklch(0.28 0.045 155);
--ink-soft:oklch(0.46 0.03 155);--green:oklch(0.42 0.09 155);--green-deep:oklch(0.33 0.08 155);
--brass:oklch(0.58 0.10 85);--brass-soft:oklch(0.88 0.04 90);--rule:oklch(0.84 0.022 100);
--serif:'Besley',Georgia,serif;--sans:'Atkinson Hyperlegible',system-ui,sans-serif;
--sp-xs:4px;--sp-sm:8px;--sp-md:16px;--sp-lg:24px;--sp-xl:48px;--sp-2xl:96px;
--mut:var(--ink-soft);--line:var(--rule);--good:var(--green-deep);--bad:oklch(0.45 0.13 30);--accent:var(--green)}}
*{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%;background:var(--paper)}}
body{{margin:0;background:var(--paper);color:var(--ink);
font:400 15px/1.55 var(--sans);font-variant-numeric:tabular-nums}}
header,main{{max-width:1000px;margin:0 auto;padding:0 20px}}
header{{padding-top:28px}}
header::after{{content:"";display:block;border-top:1px solid var(--ink);border-bottom:1px solid var(--ink);
height:3px;margin-top:var(--sp-md)}}
h1{{font:600 1.85rem/1.15 var(--serif);letter-spacing:0;margin:8px 0 4px;color:var(--ink)}}
h2{{font:700 .8rem/1 var(--sans);letter-spacing:.11em;text-transform:uppercase;
color:var(--brass);margin:0 0 var(--sp-sm)}}
h2::after{{content:"";display:block;height:1px;background:var(--rule);margin-top:var(--sp-sm)}}
.sub{{color:var(--ink-soft);font-size:.86rem}} a{{color:var(--green);text-decoration-color:color-mix(in oklch,var(--green) 40%,transparent)}}
a:hover{{color:var(--green-deep)}}
.back{{font-size:13px;text-decoration:none;font-weight:700}}
section{{background:transparent;border:0;border-top:1px solid var(--rule);border-radius:0;
padding:18px 0 0;margin:34px 0 0}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}}
.kpi{{background:var(--paper);border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);
border-radius:0;padding:12px 2px 13px}}
.kpi-v{{font:600 1.45rem/1.05 var(--serif);color:var(--green-deep);font-variant-numeric:tabular-nums}}
.kpi-l{{font:700 .72rem/1.1 var(--sans);letter-spacing:.08em;text-transform:uppercase;
color:var(--ink);margin-top:6px}}
.rc-par{{font:.88rem/1 var(--sans);color:var(--ink-soft);font-weight:400}}
table{{width:100%;border-collapse:collapse;font-size:.88rem;margin:var(--sp-md) 0;
font-variant-numeric:tabular-nums}}
thead th{{font:700 .72rem/1 var(--sans);letter-spacing:.09em;text-transform:uppercase;
color:var(--ink-soft);border-top:3px double var(--ink);border-bottom:1px solid var(--ink);
padding:10px 10px 8px;text-align:right}}
td{{text-align:right;padding:9px 10px;border-bottom:1px solid var(--rule);vertical-align:top}}
th:first-child,td:first-child{{text-align:left;padding-left:2px}}
th:last-child,td:last-child{{padding-right:2px}}
tbody tr:hover{{background:var(--paper-deep)}}
#map{{height:480px;border:1px solid var(--rule);border-radius:2px;background:var(--paper-deep)}}
.note{{color:var(--ink-soft);font-size:.8rem}}
.holenav{{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}}
.holenav button{{background:transparent;color:var(--ink-soft);border:0;border-bottom:2px solid transparent;
border-radius:0;min-width:30px;padding:4px 8px;cursor:pointer;font-size:12.5px;font-weight:700}}
.holenav button:hover{{color:var(--green-deep)}}
.holenav button.on{{background:transparent;color:var(--ink);border-bottom-color:var(--brass)}}
.legend{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px;font-size:11.5px;color:var(--ink-soft)}}
.lchip{{display:inline-flex;align-items:center;gap:4px}}
.lchip i{{width:11px;height:11px;border-radius:2px;display:inline-block}}
.explorer{{display:grid;grid-template-columns:2fr 1fr;gap:12px}}
@media(max-width:720px){{.explorer{{grid-template-columns:1fr}}}}
.shotlist{{background:var(--paper-deep);border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);
border-radius:0;padding:10px 2px;font-size:12.5px;max-height:480px;overflow:auto}}
.sl-h{{font-weight:700;margin-bottom:6px;color:var(--ink)}}
.shotlbl{{color:var(--ink);background:var(--paper);border:1px solid var(--rule);
border-radius:2px;padding:1px 4px;font-size:10px;font-weight:700;
white-space:nowrap;pointer-events:none}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.95em;color:var(--green-deep)}}
.pos{{color:var(--green-deep)}} .neg{{color:var(--bad)}}
</style></head><body>
<header>
<a class="back" href="../index.html">← back to dashboard</a>
{f'<a class="back" href="{r["pdf"]}" style="margin-left:16px">⬇ shot-map PDF</a>' if r.get("pdf") else ""}
<h1>{_esc(r['course'])} — {_esc(r['date'])}</h1>
<div class="sub">{_esc(r['tee'])} tee · {_num(r['yards'],0)} yd · par {r['par']}{weather}</div>
</header>
<main>
<section><h2>Round</h2><div class="kpis">{kpi_html}</div></section>
<section><h2>Strokes gained (vs scratch)</h2><div class="kpis">{sg_html}</div></section>
<section><h2>Shot map — explore hole by hole</h2>
<div class="holenav" id="holenav"></div>
<div class="legend" id="legend"></div>
<div class="explorer">
  <div id="map"></div>
  <div class="shotlist" id="shotlist"></div>
</div>
<p class="note">Pick a hole to zoom in — each line is one shot, colored by club, with
the club + carry labeled. Hover a shot for the detail; the panel lists every shot,
club, lie, and distance-to-pin. "All" shows the whole round.</p></section>
<section><h2>Hole by hole</h2>
<table><thead><tr><th>Hole</th><th>Par</th><th>Yd</th><th>Shots</th><th>±Par</th>
<th>Putts</th><th>FW</th><th>GIR</th><th>Tee</th><th>Drive</th><th>Prox</th><th>SG</th></tr></thead>
<tbody>{hrows}</tbody></table></section>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var SM={shotmap_json};
var ROOT_STYLE=getComputedStyle(document.documentElement);
function tok(n){{return ROOT_STYLE.getPropertyValue(n).trim();}}
var COL={{Driver:tok('--green-deep'),Wood:tok('--brass'),Hybrid:tok('--brass-soft'),
Iron:tok('--green'),Wedge:tok('--ink-soft'),Putter:tok('--ink')}};
function colOf(c){{return COL[c]||tok('--ink-soft');}}
function abbr(c){{return (c||'').replace('Pitching Wedge','PW').replace(' Wedge','°W')
 .replace(' Iron','i').replace(' Wood','W').replace('Driver','Dr').replace('Hybrid','Hy')
 .replace('Putter','Putt');}}
if(!SM.length){{
 document.getElementById('map').innerHTML='<p class="note" style="padding:20px">No GPS for this round.</p>';
}}else{{
var map=L.map('map',{{scrollWheelZoom:false}});
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
 {{maxZoom:19,attribution:'Esri'}}).addTo(map);
var layer=L.layerGroup().addTo(map);
// escape data-derived strings before they enter HTML sinks (tooltips/labels/innerHTML)
function esc(s){{return String(s==null?'':s).replace(/[&<>"']/g,function(m){{
 return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m];}});}}
function drawShot(sh,label){{
 if(!sh.s) return []; var c=colOf(sh.cat); var pts=[sh.s]; if(sh.e) pts.push(sh.e);
 var tip='#'+esc(sh.n)+' '+esc(sh.club)+' · '+esc(sh.dist)+'y'+(sh.lie?' · from '+esc(sh.lie):'');
 if(pts.length===2){{
  L.polyline(pts,{{color:c,weight:3,opacity:.95}}).addTo(layer).bindTooltip(tip);
  L.circleMarker(sh.e,{{radius:3,color:c,fillColor:c,fillOpacity:1,weight:1}}).addTo(layer);
 }}
 L.circleMarker(sh.s,{{radius:sh.tee?5:3,color:c,
  fillColor:sh.tee?tok('--paper'):c,fillOpacity:1,weight:1}}).addTo(layer).bindTooltip(tip);
 if(label && pts.length===2){{
  var mid=[(sh.s[0]+sh.e[0])/2,(sh.s[1]+sh.e[1])/2];
  L.marker(mid,{{icon:L.divIcon({{className:'shotlbl',
   html:esc(abbr(sh.club))+' '+esc(sh.dist),iconSize:[64,14]}})}}).addTo(layer);
 }}
 return pts;
}}
function show(idx){{
 layer.clearLayers(); var all=[],rows='';
 var hs = idx<0 ? SM : [SM[idx]];
 hs.forEach(function(h){{(h.shots||[]).forEach(function(sh){{
  drawShot(sh, idx>=0).forEach(function(p){{all.push(p);}});
  if(idx>=0) rows+='<tr><td style="color:'+colOf(sh.cat)+
   ';padding-left:6px">'+esc(sh.n)+'</td><td>'+esc(sh.club)+'</td><td>'+esc(sh.dist)+'y</td><td>'+
   esc(sh.lie||'')+'</td><td>'+(sh.putt?'—':esc(sh.dtp_e)+'y')+'</td></tr>';
 }});}});
 if(all.length) map.fitBounds(all,{{padding:[25,25]}});
 var sl=document.getElementById('shotlist');
 if(idx>=0){{var h=SM[idx];
  sl.innerHTML='<div class="sl-h">Hole '+esc(h.hole)+' · par '+esc(h.par||'?')+
   (h.len?' · '+Math.round(h.len)+'y':'')+'</div>'+
   '<table><thead><tr><th>#</th><th>Club</th><th>Carry</th><th>Lie</th><th>To pin</th>'+
   '</tr></thead><tbody>'+rows+'</tbody></table>';
 }}else sl.innerHTML='<div class="sl-h">All '+SM.length+' holes</div>'+
   '<p class="note">Pick a hole number above to see each shot and the club used.</p>';
}}
var nav=document.getElementById('holenav');
function setActive(b){{nav.querySelectorAll('button').forEach(function(x){{x.classList.remove('on');}});b.classList.add('on');}}
var ab=document.createElement('button');ab.textContent='All';ab.className='on';
ab.onclick=function(){{setActive(ab);show(-1);}};nav.appendChild(ab);
SM.forEach(function(h,i){{var b=document.createElement('button');b.textContent=h.hole;
 b.onclick=function(){{setActive(b);show(i);}};nav.appendChild(b);}});
var lg=document.getElementById('legend');
Object.keys(COL).forEach(function(k){{lg.innerHTML+='<span class="lchip"><i style="background:'+
 COL[k]+'"></i>'+k+'</span>';}});
show(-1);
}}
</script>
</body></html>"""


def main():
    store = sys.argv[1] if len(sys.argv) > 1 else "."
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join("docs", "index.html")
    data = compute(store)
    outdir = os.path.dirname(os.path.abspath(out))
    os.makedirs(outdir, exist_ok=True)
    _copy_font_assets(outdir)
    rounds_dir = os.path.join(outdir, "rounds")
    os.makedirs(rounds_dir, exist_ok=True)
    # detect an existing shot-map PDF per round (generated separately) so we only
    # ever link a PDF that's really there — no dead links.
    for r in data["rounds"]:
        pdf = f'{r["slug"]}_shotmaps.pdf'
        r["pdf"] = pdf if os.path.exists(os.path.join(rounds_dir, pdf)) else None
    # The dashboard HTML is the product — write it FIRST so nothing else can block it.
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_html(data, "assets/fonts"))
    # per-round full-review pages -> <outdir>/rounds/<slug>_review.html
    for r in data["rounds"]:
        with open(os.path.join(rounds_dir, f'{r["slug"]}_review.html'), "w",
                  encoding="utf-8") as f:
            f.write(render_round_page(data, r, "../assets/fonts"))
    # Persist the cleaned per-club distances as a data artifact so the outlier
    # filter / best-third numbers live IN THE DATA. Secondary — never let a failure
    # here (locked/missing store) block the dashboard above.
    try:
        if os.path.isdir(store):
            with open(os.path.join(store, "club_distances.csv"), "w", newline="",
                      encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["club", "category", "total_yd", "total_lo_yd",
                            "total_hi_yd", "carry_yd", "carry_sd_yd", "lateral_sd_yd",
                            "shots", "confidence"])
                for c in data["dispersion"]:
                    w.writerow([c["club"], c["category"], c.get("total"),
                                c.get("total_lo"), c.get("total_hi"), c["carry"],
                                c["carry_sd"], c["lateral_sd"], c["n"],
                                c["confidence"]])
    except OSError as e:
        print(f"warning: could not write club_distances.csv: {e}")
    m = data["meta"]
    print(f"wrote {out} + {len(data['rounds'])} round reviews  "
          f"({m['n_rounds']} rounds, {m['n_holes']} holes, {m['n_shots']} shots, "
          f"index {data['player']['index']})")


if __name__ == "__main__":
    main()
