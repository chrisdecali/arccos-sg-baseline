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
  * matplotlib is the only hard dependency; interactive map uses CDN (Leaflet+Esri),
    which loads client-side only when the HTML is opened in a browser.

Usage:
    python dashboard/gen_tracker.py [store_dir=.] [out=docs/index.html]

compute(store_dir) is pure (files in -> dict out) so a trend module can call it.
"""
from __future__ import annotations

import base64
import csv
import html
import io
import json
import math
import os
import random
import statistics
import sys
import zlib
from datetime import date

# matplotlib only for static charts; force a headless backend before pyplot.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

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


def _slug(course: str, date: str) -> str:
    """Stable filename slug for a round, e.g. 'WindRose_GC_2026-06-06'."""
    base = "".join(c if c.isalnum() else "_" for c in (course or "round"))
    while "__" in base:
        base = base.replace("__", "_")
    return f"{base.strip('_')}_{date or 'x'}"


def _round5(x):
    """Round to the nearest 5 yards (None-safe)."""
    return None if x is None else int(round(x / 5.0) * 5)


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
    profile = _read_json(os.path.join(store, "player_profile.json"), {})

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
    for name, target in TARGET_BAG:
        cm = club_meta.get(name, {})
        cat = cm.get("club_category") or _club_cat(name)
        mc = club_mc.get(name)
        n = mc[3] if mc else 0
        total_est = mc[0] if mc else _f(cm.get("smart_distance_yd"))
        factor = club_factor.get(name, CARRY_FACTOR.get(cat, 0.97))   # wet-aware
        measured = _round5(total_est * factor) if total_est else None
        # Use MEASURED when it's trustworthy: enough on-course samples AND within
        # ~15% of target (else it's noise/mis-tag). Otherwise trust the target.
        confident = (measured is not None and n >= 8
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
            "n": n or 0, "held": held, "low_conf": (n or 0) < 5,
        })

    # ---- lateral offsets per club (one pass; shared by dispersion + aim) ----
    pin_of = {}
    for h in holes:
        pin_of[(h.get("round_id"), h.get("hole_id"))] = (
            _f(h.get("pin_lat")), _f(h.get("pin_lng")))
    lat_by_club: dict[str, list[float]] = {}    # all shots — for dispersion spread
    lat_aim: dict[str, list[float]] = {}        # non-tee only — for aim bias
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
            # Off the tee you aim at the fairway/dogleg, not the pin — so the offset
            # to the pin line isn't "aim bias". Only non-tee shots inform aim.
            if not _truthy(s.get("is_tee")):
                lat_aim.setdefault(club, []).append(off)

    # ---- dispersion explorer (recency-weighted best-third, MC; wet-aware carry) ----
    bag_order = {name: i for i, (name, _t) in enumerate(TARGET_BAG)}
    disp_clubs = []
    for name, sh in shots_by_club.items():
        mc = club_mc.get(name)
        if not mc:
            continue
        med, lo, hi, n = mc                       # recency-weighted best-third (MC)
        cat = (club_meta.get(name, {}) or {}).get("club_category") or _club_cat(name)
        factor = club_factor.get(name, CARRY_FACTOR.get(cat, 0.97))   # wet-aware
        kept = sorted(d for d, _o, _w in sh)      # for the shot-to-shot spread
        lat = _iqr_filter(lat_by_club.get(name, []))   # drop hooks/pushes
        disp_clubs.append({
            "club": name, "category": cat, "group": GROUP_OF.get(cat, "Other"),
            # total = real measured distance; carry = total x condition-aware factor
            "total": _round5(med), "total_lo": _round5(lo), "total_hi": _round5(hi),
            "carry": _round5(med * factor), "wet": club_wet.get(name, 0.0),
            # spread = SD of the club's shots (shot-to-shot consistency)
            "carry_sd": _round5(statistics.pstdev(kept) * factor) if len(kept) > 1 else 0,
            "lateral_sd": _round5(statistics.pstdev(lat)) if len(lat) > 1 else None,
            "n": n,
            "confidence": "high" if n >= 12 else "medium" if n >= 6 else "low",
        })
    disp_clubs.sort(key=lambda d: bag_order.get(d["club"], 99))   # natural club order

    # ---- aim-by-club (signed lateral bias to the pin line) ----
    aim = []
    for name, _t in TARGET_BAG:
        offs = _iqr_filter(lat_aim.get(name, []))
        if len(offs) < 3:
            continue
        bias = statistics.fmean(offs)
        nb = len(offs)
        if nb < 6:
            rec = "need more data"          # don't recommend an aim change off <6
        elif abs(bias) >= 5:
            rec = "aim %s" % ("left" if bias > 0 else "right")
        else:
            rec = "on line"
        aim.append({
            "club": name, "bias": round(bias, 1), "n": nb,
            "side": "right" if bias > 0 else "left", "rec": rec,
        })

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

    # ---- per-round hole detail (for the full round-review pages) ----
    holes_by_round: dict[str, list[dict]] = {}
    for h in holes:
        holes_by_round.setdefault(h.get("round_id"), []).append({
            "hole": _i(h.get("hole_id")), "par": _i(h.get("par")),
            "len": _f(h.get("hole_len_yd")), "shots": _i(h.get("shots")),
            "to_par": _i(h.get("score_to_par")), "putts": _i(h.get("putts")),
            "fairway": _truthy(h.get("fairway_hit")), "gir": _truthy(h.get("gir")),
            "drive": _f(h.get("drive_yd")),
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
        "dispersion": disp_clubs,
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


# ----------------------------------------------------------------- charts (PNG)
def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def chart_sg_by_round(rounds) -> str:
    cats = [("sg_off_tee", "#2e7d32"), ("sg_approach", "#1565c0"),
            ("sg_short", "#ef6c00"), ("sg_putting", "#6a1b9a")]
    labels = [r["date"] for r in rounds]
    fig, ax = plt.subplots(figsize=(7, 3.2), facecolor="white")
    x = range(len(rounds))
    bottom_pos = [0.0] * len(rounds)
    bottom_neg = [0.0] * len(rounds)
    for key, color in cats:
        vals = [r.get(key) or 0 for r in rounds]
        bottoms = [bottom_neg[i] if v < 0 else bottom_pos[i]
                   for i, v in enumerate(vals)]
        ax.bar(x, vals, bottom=bottoms, color=color,
               label=key.replace("sg_", "").replace("_", " "))
        for i, v in enumerate(vals):
            if v < 0:
                bottom_neg[i] += v
            else:
                bottom_pos[i] += v
    ax.axhline(0, color="#444", lw=0.8)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("Strokes gained vs scratch")
    ax.legend(fontsize=7, ncol=4, loc="lower center", frameon=False)
    ax.set_title("Strokes gained by round (Arccos, vs scratch)", fontsize=10)
    ax.grid(axis="y", alpha=0.25)
    return _png(fig)


def chart_dispersion(disp_clubs) -> str:
    pts = [(d["carry"], d["lateral_sd"], d["club"], d["confidence"])
           for d in disp_clubs if d["carry"] and d["lateral_sd"]]
    if not pts:
        return ""
    cmap = {"high": "#2e7d32", "medium": "#f9a825", "low": "#c62828"}
    fig, ax = plt.subplots(figsize=(7, 3.6), facecolor="white")
    for carry, lat, club, conf in pts:
        ax.scatter(carry, lat, s=44, color=cmap.get(conf, "#888"), zorder=3)
        ax.annotate(club, (carry, lat), fontsize=7,
                    xytext=(4, 3), textcoords="offset points")
    ax.set_xlabel("Carry (yds, modeled)")
    ax.set_ylabel("Lateral spread  (1 SD, yds)")
    ax.set_title("Dispersion by club — lower is tighter", fontsize=10)
    ax.grid(alpha=0.25)
    handles = [plt.Line2D([0], [0], marker="o", ls="", color=c, label=k)
               for k, c in cmap.items()]
    ax.legend(handles=handles, title="confidence", fontsize=7, title_fontsize=7,
              frameon=False)
    return _png(fig)


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


def render_html(d: dict) -> str:
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

    # ---- bag table ----
    bag_rows = ""
    for b in d["bag"]:
        flag = ' <span class="hold" title="held to keep the bag descending / low sample">hold</span>' if b["held"] else ""
        lc = ' <span class="lc" title="usage_count &lt; 5 — noisy">low n</span>' if b["low_conf"] else ""
        meas = _num(b["measured"], 0) if b["measured"] else "—"
        bag_rows += (
            f'<tr><td>{_esc(b["club"])}</td><td>{b["target"]}</td>'
            f'<td>{meas}{lc}</td><td><b>{b["suggested"]}</b>{flag}</td>'
            f'<td>{b["n"]}</td></tr>')

    # ---- dispersion table ----
    disp_rows = ""
    for c in d["dispersion"]:
        # Monte-Carlo 80% band on the total — shows how (un)certain the estimate is
        rng = (f' <span class="rng" title="Monte-Carlo 80% range from your shots">'
               f'{_num(c.get("total_lo"),0)}–{_num(c.get("total_hi"),0)}</span>'
               if c.get("total_lo") is not None else "")
        disp_rows += (
            f'<tr data-group="{_esc(c["group"])}"><td>{_esc(c["club"])}</td>'
            f'<td><b>{_num(c["total"],0)}</b>{rng}</td><td>{_num(c["carry"],0)}</td>'
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
            '<p class="note">Bias measured to the pin line — on doglegs you aim at '
            'the bend, not the flag, so treat those as noise. Confirm push-vs-aim on '
            'a launch monitor; never aim into a hazard.</p>')
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
    sg_png = chart_sg_by_round(d["rounds"]) if d["rounds"] else ""
    disp_png = chart_dispersion(d["dispersion"]) if d["dispersion"] else ""
    map_json = json.dumps(d["map"])

    plan_html = "".join(f"<li>{x}</li>" for x in plan)
    courses = ", ".join(_esc(c) for c in m["courses"]) or "—"

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strokes-Gained Tracker — Chris Cole</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
:root{{--bg:#0f1115;--card:#1a1d24;--ink:#e8eaed;--mut:#9aa0aa;--line:#2a2e37;
--good:#43a047;--bad:#e53935;--accent:#4f9cf9}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}}
header{{padding:24px 20px 8px;max-width:1000px;margin:0 auto}}
h1{{margin:0;font-size:24px}} h2{{font-size:18px;margin:0 0 12px}}
.sub{{color:var(--mut);font-size:13px}}
main{{max-width:1000px;margin:0 auto;padding:8px 20px 60px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 18px 20px;margin:16px 0}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
@media(max-width:640px){{.kpis{{grid-template-columns:repeat(2,1fr)}}}}
.kpi{{background:#0f131a;border:1px solid var(--line);border-radius:10px;padding:12px}}
.kpi-v{{font-size:22px;font-weight:700}} .kpi-l{{font-size:13px;margin-top:2px}}
.kpi-s{{font-size:11px;color:var(--mut)}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}}
td b{{color:var(--accent)}}
.note{{color:var(--mut);font-size:12.5px;margin:10px 0 0}}
.hold,.lc{{font-size:10px;padding:1px 5px;border-radius:6px;background:#3a2c12;color:#f3c969}}
.lc{{background:#3a1f1f;color:#f3a0a0}}
.rng{{font-size:10px;color:var(--mut)}}
.conf{{font-size:11px;padding:1px 7px;border-radius:10px}}
.conf-high{{background:#1b3a22;color:#7fd18c}} .conf-medium{{background:#3a3212;color:#e8d27a}}
.conf-low{{background:#3a1f1f;color:#f3a0a0}}
ul{{margin:0;padding-left:18px}} li{{margin:6px 0}}
img.chart{{width:100%;height:auto;border-radius:8px;background:#fff;padding:6px}}
.tabs button{{background:#0f131a;color:var(--ink);border:1px solid var(--line);
border-radius:8px;padding:6px 12px;margin-right:6px;cursor:pointer}}
.tabs button.on{{background:var(--accent);color:#06121f;border-color:var(--accent)}}
#map{{height:440px;border-radius:10px;margin-top:10px}}
.slider-row{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:10px 0}}
input[type=range]{{flex:1;min-width:180px}}
.proj{{font-size:22px;font-weight:700;color:var(--accent)}}
.pos{{color:var(--good)}} .neg{{color:var(--bad)}}
.modeled{{border-bottom:1px dotted var(--mut);cursor:help}}
.course-grp{{margin:14px 0}}
.course-h{{font-size:15px;margin:0 0 8px}} .course-n{{color:var(--mut);font-size:12px;font-weight:400}}
.rcards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}}
.rcard-wrap{{display:flex;flex-direction:column;gap:4px}}
.rcard{{display:block;text-decoration:none;color:var(--ink);background:#0f131a;
border:1px solid var(--line);border-radius:10px;padding:12px 14px;transition:.12s}}
.rcard:hover{{border-color:var(--accent);transform:translateY(-2px)}}
.rc-pdf{{font-size:11.5px;color:var(--mut);text-decoration:none;padding-left:4px}}
.rc-pdf:hover{{color:var(--accent)}}
.rc-date{{font-size:12px;color:var(--mut)}}
.rc-score{{font-size:26px;font-weight:700;margin:2px 0}}
.rc-par{{font-size:14px;color:var(--mut);font-weight:400}}
.rc-meta{{font-size:11.5px;color:var(--mut)}}
.rc-sg{{font-size:13px;font-weight:600;margin-top:6px}}
.rc-open{{font-size:12px;color:var(--accent);margin-top:6px}}
footer{{max-width:1000px;margin:0 auto;padding:0 20px 50px;color:var(--mut);font-size:12px}}
</style></head>
<body>
<header>
<h1>⛳ Strokes-Gained Tracker</h1>
<div class="sub">Chris Cole · {courses} · {m['n_rounds']} rounds · {m['n_holes']} holes ·
{m['n_shots']} shots · index <b>{_num(p['index'],1)}</b> ·
generated {_esc(m['generated_at'])}</div>
</header>
<main>

<section><h2>Game plan</h2><ul>{plan_html}</ul>
<p class="note">Targets for PGA Frisco (Oct 21–24, 2026). All strokes-gained is
Arccos, measured vs scratch — large negatives are normal for a mid-handicap.</p></section>

<section><h2>Key numbers</h2><div class="kpis">{kpi_html}</div></section>

<section><h2>Rounds — full reviews</h2>
<p class="note">Click any round to open its full review: satellite shot map,
hole-by-hole, and strokes-gained for that round.</p>
{nav_html or '<p class="note">No rounds yet.</p>'}</section>

<section><h2>Index projection</h2>
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

<section><h2>Strokes gained by round</h2>
{'<img class="chart" src="'+sg_png+'">' if sg_png else '<p class="note">No rounds yet.</p>'}
</section>

<section><h2>Cost of misses</h2>
<table><thead><tr><th>Category</th><th>SG / round</th><th>Lever</th></tr></thead>
<tbody>{lever_rows}</tbody></table>
<p class="note">Recoverable if you stopped bleeding strokes in the negative
categories: <b>{d['recoverable']['raw']}</b> raw. SG levers overlap ~35–40%, so the
realistic combined gain is about <b>{d['recoverable']['effective']}</b> strokes/round
(0.62 efficiency factor — don't add them straight up).</p></section>

<section><h2>Approach & putting</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:18px" class="twocol">
 <div><h3 style="font-size:14px;color:var(--mut)">Approach SG by pin distance</h3>
 <table><thead><tr><th>Band</th><th>SG</th><th>n</th></tr></thead>
 <tbody>{appr_rows or '<tr><td colspan=3>—</td></tr>'}</tbody></table></div>
 <div><h3 style="font-size:14px;color:var(--mut)">Putting SG by length</h3>
 <table><thead><tr><th>Length</th><th>SG</th><th>n</th></tr></thead>
 <tbody>{putt_rows or '<tr><td colspan=3>—</td></tr>'}</tbody></table></div>
</div></section>

<section><h2>Scrambling — the priority</h2>
<table><thead><tr><th>Chip distance</th><th>Avg proximity</th><th>Target</th></tr></thead>
<tbody>{chip_rows or '<tr><td colspan=3>—</td></tr>'}</tbody></table>
<p class="note">Chip save {_pct(k['chip_save_pct'])} · sand save
{_pct(k['sand_save_pct'])} · chip error {_pct(k['chip_error_rate'])}. The miss is
<b>contact</b>, not the putt that follows — proximity is measured from where the chip
finishes. Fix the chunk first.</p></section>

<section><h2>Aim by club</h2>{aim_block}</section>

<section><h2>Dispersion explorer</h2>
{'<img class="chart" src="'+disp_png+'">' if disp_png else ''}
<div class="tabs" id="disp-tabs" style="margin:12px 0 8px">
 <button class="on" data-g="all">All</button><button data-g="Woods">Woods</button>
 <button data-g="Irons">Irons</button><button data-g="Wedges">Wedges</button></div>
<table><thead><tr><th>Club</th><th>Total (best ⅓)</th><th>Carry</th><th>Carry ±SD</th>
<th>Lateral ±SD</th><th>Shots</th><th>Confidence</th></tr></thead>
<tbody id="disp-body">{disp_rows}</tbody></table>
<p class="note"><b>Total</b> is your real measured distance (carry + roll) from Arccos —
the <b>best-third</b> strike, <b>recency-weighted</b> (recent rounds count more), and
<b>Monte-Carlo bootstrapped</b> (the small grey range is the 80% band, so a thin sample
reads as uncertain). <b>Carry</b> = total × a roll factor that's
<b>adjusted for conditions</b> — your rounds are played wet (rain/drizzle), so the
ground barely rolls and carry sits just under total (it would subtract more on firm,
dry turf). Still modeled until <i>launch-monitor carries (Tee Box, July)</i> become the
source of truth. Rounded to 5 yds; also in <code>club_distances.csv</code>.</p></section>

<section><h2>Measured vs target bag</h2>
<table><thead><tr><th>Club</th><th>Target carry</th><th>Measured (best-⅓)</th>
<th>Suggested</th><th>n</th></tr></thead><tbody>{bag_rows}</tbody></table>
<p class="note">Target = your 18Birdies carry set. Measured = your recency-weighted,
Monte-Carlo best-third <b>carry</b> (total × roll factor — modeled until launch-monitor
data lands in July). The bag stays <b>strictly descending</b>: a "hold" tag means a
noisy data point would have broken club order, so the target stands.</p></section>

<section><h2>Round map</h2>
<div class="tabs" id="round-tabs"></div>
<div id="map"></div>
<p class="note">Satellite tiles (Esri) load in the browser only. Each line is a hole's
shot path from GPS. No GPS on a round → it won't appear here.</p></section>

<section><h2>Trouble holes</h2>
<table><thead><tr><th>Hole</th><th>Par</th><th>Length</th><th>Avg vs par</th>
<th>n</th></tr></thead><tbody>{trouble_rows}</tbody></table>
<p class="note">Needs ~5+ rounds before this is signal rather than noise.</p></section>

<section><h2>Posted scores (GHIN)</h2>
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
// trajectory, not the official number (which is shown above as 14.1).
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

// ---- round map (Esri satellite) ----
var ROUNDS={map_json};
var map=L.map('map',{{scrollWheelZoom:false}});
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
 {{maxZoom:19,attribution:'Esri'}}).addTo(map);
var layer=L.layerGroup().addTo(map);
function drawRound(r){{
 layer.clearLayers(); var all=[];
 (r.holes||[]).forEach(function(h){{
  if(h.pts.length<2) return;
  var pl=L.polyline(h.pts,{{color:'#ffd54f',weight:2,opacity:.9}}).addTo(layer);
  h.pts.forEach(p=>all.push(p));
  L.circleMarker(h.pts[0],{{radius:3,color:'#4f9cf9',fillOpacity:1}})
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
</script>
</body></html>"""


def render_round_page(d: dict, r: dict) -> str:
    """Full review for one round: satellite shot map + hole-by-hole + SG."""
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
        f'<td>{"✓" if h["gir"] else ""}</td><td>{_num(h["drive"],0)}</td>'
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
:root{{--bg:#0f1115;--card:#1a1d24;--ink:#e8eaed;--mut:#9aa0aa;--line:#2a2e37;
--good:#43a047;--bad:#e53935;--accent:#4f9cf9}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}}
header,main{{max-width:1000px;margin:0 auto;padding:0 20px}}
header{{padding-top:22px}} h1{{margin:6px 0 2px;font-size:22px}}
.sub{{color:var(--mut);font-size:13px}} a{{color:var(--accent)}}
.back{{font-size:13px;text-decoration:none}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;margin:16px 0}} h2{{font-size:17px;margin:0 0 12px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}}
.kpi{{background:#0f131a;border:1px solid var(--line);border-radius:10px;padding:11px}}
.kpi-v{{font-size:20px;font-weight:700}} .kpi-l{{font-size:12px;color:var(--mut)}}
.rc-par{{font-size:13px;color:var(--mut);font-weight:400}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-size:11px;text-transform:uppercase}}
#map{{height:480px;border-radius:10px}}
.note{{color:var(--mut);font-size:12px}}
.holenav{{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}}
.holenav button{{background:#0f131a;color:var(--ink);border:1px solid var(--line);
border-radius:7px;min-width:30px;padding:4px 8px;cursor:pointer;font-size:12.5px}}
.holenav button.on{{background:var(--accent);color:#06121f;border-color:var(--accent)}}
.legend{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px;font-size:11.5px;color:var(--mut)}}
.lchip{{display:inline-flex;align-items:center;gap:4px}}
.lchip i{{width:11px;height:11px;border-radius:2px;display:inline-block}}
.explorer{{display:grid;grid-template-columns:2fr 1fr;gap:12px}}
@media(max-width:720px){{.explorer{{grid-template-columns:1fr}}}}
.shotlist{{background:#0f131a;border:1px solid var(--line);border-radius:10px;
padding:10px;font-size:12.5px;max-height:480px;overflow:auto}}
.sl-h{{font-weight:600;margin-bottom:6px}}
.shotlbl{{color:#fff;font-size:10px;font-weight:600;
text-shadow:0 0 3px #000,0 0 3px #000;white-space:nowrap;pointer-events:none}}
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
<th>Putts</th><th>FW</th><th>GIR</th><th>Drive</th><th>Prox</th><th>SG</th></tr></thead>
<tbody>{hrows}</tbody></table></section>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var SM={shotmap_json};
var COL={{Driver:'#e53935',Wood:'#fb8c00',Hybrid:'#fdd835',Iron:'#43a047',
Wedge:'#29b6f6',Putter:'#ab47bc'}};
function colOf(c){{return COL[c]||'#bbbbbb';}}
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
  fillColor:sh.tee?'#ffffff':c,fillOpacity:1,weight:1}}).addTo(layer).bindTooltip(tip);
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
  if(idx>=0) rows+='<tr><td style="border-left:3px solid '+colOf(sh.cat)+
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
    rounds_dir = os.path.join(outdir, "rounds")
    os.makedirs(rounds_dir, exist_ok=True)
    # detect an existing shot-map PDF per round (generated separately) so we only
    # ever link a PDF that's really there — no dead links.
    for r in data["rounds"]:
        pdf = f'{r["slug"]}_shotmaps.pdf'
        r["pdf"] = pdf if os.path.exists(os.path.join(rounds_dir, pdf)) else None
    # The dashboard HTML is the product — write it FIRST so nothing else can block it.
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_html(data))
    # per-round full-review pages -> <outdir>/rounds/<slug>_review.html
    for r in data["rounds"]:
        with open(os.path.join(rounds_dir, f'{r["slug"]}_review.html'), "w",
                  encoding="utf-8") as f:
            f.write(render_round_page(data, r))
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
