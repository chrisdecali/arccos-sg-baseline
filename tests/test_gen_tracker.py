#!/usr/bin/env python3
"""
Tests for gen_tracker.py — guards the math that keeps biting (index, distances,
bag ordering, geometry sign). Dependency-free: runs with plain `python`, no pytest
needed (though `pytest tests/` also works). Run:

    .venv\\Scripts\\python tests\\test_gen_tracker.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "dashboard"))
import gen_tracker as g  # noqa: E402


# --------------------------------------------------------------- pure functions
def test_round5():
    assert g._round5(243) == 245
    assert g._round5(242) == 240
    assert g._round5(270.4) == 270
    assert g._round5(0) == 0
    assert g._round5(None) is None


def test_whs_index_known_values():
    # 4 scores -> lowest 1 minus 1.0 adjustment
    assert g.whs_index([14.1, 17.7, 21.3, 22.6]) == 13.1
    # 3 scores -> lowest 1 minus 2.0
    assert g.whs_index([10.0, 20.0, 30.0]) == 8.0
    # 5 scores -> lowest 1, no adjustment
    assert g.whs_index([14.1, 17.7, 21.3, 22.6, 9.0]) == 9.0
    # 6 scores -> avg lowest 2 minus 1.0
    assert g.whs_index([10.0, 12.0, 20.0, 22.0, 24.0, 26.0]) == 10.0
    # too few scores -> None
    assert g.whs_index([12.0, 15.0]) is None


def test_sg_benchmark_interpolates_monotonic_and_clamps():
    assert g._sg_benchmark(-4) == g._sg_benchmark(0)
    assert g._sg_benchmark(40) == g._sg_benchmark(25)
    mid = g._sg_benchmark(7.5)
    assert mid["drive"] == -0.95
    assert mid["approach"] == -2.3
    assert mid["short"] == -0.95
    assert mid["putt"] == -0.75
    prev = g._sg_benchmark(0)
    for hcp in [1, 5, 7.5, 10, 15, 18, 20, 25]:
        cur = g._sg_benchmark(hcp)
        for facet in ("drive", "approach", "short", "putt"):
            assert cur[facet] <= prev[facet]
        prev = cur


def test_round_story_from_round_data_contains_score():
    r = {
        "course": "Test Links", "date": "2026-07-01", "score": 82, "to_par": 10,
        "sg_off_tee": 1.2, "sg_approach": -4.4, "sg_short": -0.5,
        "sg_putting": 0.3, "gir": 39.0, "fairway": 57.0,
    }
    holes = [
        {"hole": 1, "to_par": -1, "putts": 1, "penalties": 0, "sg": 1.6, "drive": 268},
        {"hole": 2, "to_par": 2, "putts": 3, "penalties": 1, "sg": -2.2, "drive": 242},
    ]
    story = g._round_story(r, holes)
    assert story
    assert "82" in story
    assert "off the tee" in story
    assert "approach" in story
    assert "#1" in story and "#2" in story


def test_slug_ascii_folds_accented_course_names():
    assert g._slug("Café São João", "2026-07-10") == "Cafe_Sao_Joao_2026-07-10"
    assert g._slug("東京", "2026-07-10") == "round_2026-07-10"


def test_round_slugs_are_unique_for_same_course_and_date():
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmp, "rounds_summary.csv"), "w", encoding="utf-8") as fh:
            fh.write("round_id,date,course,score,score_to_par\n")
            fh.write("r1,2026-07-04,Same Course,78,6\n")
            fh.write("r2,2026-07-04,Same Course,80,8\n")
        data = g.compute(tmp)
        assert [r["slug"] for r in data["rounds"]] == [
            "Same_Course_2026-07-04",
            "Same_Course_2026-07-04_2",
        ]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_clean_third_best_strike():
    # best third = the longest third, averaged
    assert g._clean_third([100, 200, 300]) == 300          # top 1 of 3
    assert g._clean_third([10, 20, 30, 40, 50, 60]) == 55  # top 2 of 6 -> (50+60)/2
    assert g._clean_third([]) is None


def test_iqr_filter_drops_outlier():
    vals = [10, 11, 12, 13, 100]   # 100 is a clear outlier
    out = g._iqr_filter(vals)
    assert 100 not in out
    assert set(out) == {10, 11, 12, 13}
    # too few points -> returned unchanged (no filtering)
    assert g._iqr_filter([5, 7]) == [5, 7]


def test_lateral_offset_sign_convention():
    # Standing at start facing the pin: a shot ending to the player's RIGHT is +,
    # LEFT is -. Verify for two different target bearings so the formula isn't
    # accidentally bearing-dependent.
    start = (30.0000, -95.0000)
    pin_north = (30.0200, -95.0000)   # pin due north
    end_east = (30.0000, -94.9800)    # end due east  -> right of a north shot
    end_west = (30.0000, -95.0200)    # end due west  -> left
    assert g._lateral_offset(start, end_east, pin_north) > 0   # right
    assert g._lateral_offset(start, end_west, pin_north) < 0   # left

    pin_east = (30.0000, -94.9800)    # pin due east
    end_north = (30.0200, -95.0000)   # end due north -> left of an east shot
    assert g._lateral_offset(start, end_north, pin_east) < 0   # left

    # missing data -> None (not a crash, not a fake 0)
    assert g._lateral_offset(start, end_east, (None, None)) is None


def test_enu_yards_directions():
    e, n = g._enu_yards(30.01, -95.0, 30.0, -95.0)   # due north
    assert n > 0 and abs(e) < 1
    e, n = g._enu_yards(30.0, -94.99, 30.0, -95.0)   # due east
    assert e > 0 and abs(n) < 1


def test_wet_classifier_and_roll_factor():
    assert g._is_wet("Drizzle") and g._is_wet("Rain") and g._is_wet("Thunderstorm")
    assert not g._is_wet("Clear") and not g._is_wet("Partly cloudy") and not g._is_wet("")
    # wet ground -> less roll -> carry factor closer to 1 than dry
    dry = g._roll_factor("Driver", 0.0)
    wet = g._roll_factor("Driver", 1.0)
    assert wet > dry                       # wet ground rolls less -> factor nearer 1
    assert wet == g.WET_FACTOR["Driver"] and dry == g.CARRY_FACTOR["Driver"]
    # Integration: a club's carry == total x the roll factor blended for HOW WET its
    # shots actually were — deterministic regardless of the wet/dry mix in the store.
    d = _compute()
    drv = [c for c in d["dispersion"] if c["club"] == "Driver"][0]
    expected = g._round5(drv["total"] * g._roll_factor("Driver", drv["wet"]))
    assert drv["carry"] == expected


def test_miss_vs_sign_convention():
    # target due north of start; +long = past target, +right = right of the line
    start = (30.0000, -95.0000)
    target = (30.0200, -95.0000)
    past = (30.0300, -95.0000)     # beyond the target -> long
    short = (30.0100, -95.0000)    # not reached -> short
    east = (30.0200, -94.9900)     # at target depth but east -> right
    assert g._miss_vs(start, past, target)[0] > 0     # long
    assert g._miss_vs(start, short, target)[0] < 0    # short
    assert g._miss_vs(start, east, target)[1] > 0     # right
    assert g._miss_vs(start, (30.02, -95.01), target)[1] < 0   # left
    assert g._miss_vs(start, past, (None, None)) is None


def test_shot_patterns_present():
    d = _compute()
    ap = d["patterns"]["approach"]
    assert ap["overall"] and ap["overall"]["n"] > 0
    # every club row has both miss axes + outlier-exclusion bookkeeping
    for c in ap["by_club"]:
        assert "ls" in c and "lr" in c and c["n"] >= 4
        assert c["used"] <= c["n"]            # some shots may be dropped as outliers


def test_driving_accuracy():
    d = _compute()
    assert d["driving"], "driving accuracy should be populated from fairway flags"
    for dd in d["driving"]:
        # fairway + miss-left + miss-right should roughly account for the tee shots
        assert 0 <= dd["fw_pct"] <= 100
        assert dd["fw_pct"] + dd["left_pct"] + dd["right_pct"] <= 101  # rounding slack
        assert dd["chances"] >= 3


def test_coaching_recent_and_byclub():
    d = _compute()
    co = d["coaching"]
    # recency split: all 4 SG categories compared macro vs recent
    cats = [c["cat"] for c in co["sg_compare"]]
    assert {"Off the tee", "Approach", "Short game", "Putting"} <= set(cats)
    for c in co["sg_compare"]:
        if c["macro"] is not None and c["recent"] is not None:
            assert c["delta"] == round(c["recent"] - c["macro"], 1)
    # per-club coaching covers driver (woods) and at least one iron
    clubs = [c["club"] for c in co["by_club"]]
    assert "Driver" in clubs and any("Iron" in c for c in clubs)
    # the standalone Game-plan cards were removed (cannibalized by the Top 10)
    assert "macro" not in co
    # the systemic swing/hook fix now lives as the #1 Top-10 action
    assert "hook" in co["top10"][0]["action"].lower()


def test_launch_monitor_lights_up_swing_and_carry():
    # Dormant until logged: a Trackman session (face closed to path) must (a) flip that
    # club's bag number to MEASURED and (b) add a measured face-to-path line to the
    # swing-pattern card. Guards the "waiting for Tee Box" wiring.
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    for f in os.listdir(REPO):
        s = os.path.join(REPO, f)
        if os.path.isfile(s):
            shutil.copy(s, tmp)
    hdr = ("date,club,carry_yd,total_yd,club_mph,ball_mph,smash,launch_deg,launch_dir_deg,"
           "spin_rpm,spin_axis_deg,attack_deg,club_path_deg,face_angle_deg,face_to_path_deg,notes\n")
    with open(os.path.join(tmp, "launch_monitor.csv"), "w") as fh:
        fh.write(hdr)
        for club, carry, f2p in [("7 Iron", 168, -4.5), ("5 Iron", 190, -3.8),
                                 ("Driver", 268, -5.2)]:
            fh.write(f"2026-07-12,{club},{carry},,86,123,1.43,17,1,6000,-3,-3,2,-2,{f2p},t\n")
    d = g.compute(tmp)
    assert any(b["club"] == "7 Iron" and b.get("measured_src") for b in d["bag"])
    # the measured face-to-path now enriches the #1 Top-10 (hook) action
    hook = d["coaching"]["top10"][0]
    assert "hook" in hook["action"].lower()
    assert "Measured" in hook["detail"] and "closed to the" in hook["detail"]
    shutil.rmtree(tmp, ignore_errors=True)


def test_top10_actions_ranked_with_sg():
    d = _compute()
    t = d["coaching"]["top10"]
    assert 1 <= len(t) <= 10
    # every action carries a positive SG estimate and a rank
    for i, x in enumerate(t, 1):
        assert x["rank"] == i and x["sg"] > 0 and x["action"] and x["cat"]
    # strictly ranked by strokes gained (descending)
    sgs = [x["sg"] for x in t]
    assert sgs == sorted(sgs, reverse=True)
    # the swing/hook fix is the #1 leverage item for this player
    assert "hook" in t[0]["action"].lower()


def test_iqr_bounds_flags_outliers():
    lo, hi = g._iqr_bounds([10, 11, 12, 13, 14, 200])   # 200 is an outlier
    assert not (lo <= 200 <= hi)              # 200 is outside the fence
    assert lo <= 12 <= hi                     # the body is inside
    assert g._iqr_bounds([1, 2]) == (-1e9, 1e9)   # too few -> no filtering


def test_recency_weight_decays():
    # a more recent shot weighs more; one half-life back weighs ~0.5
    newest = g.date(2026, 6, 20).toordinal()
    assert g._recency_weight(newest, newest) == 1.0
    half = g.date(2026, 6, 20).toordinal() - g.RECENCY_HALF_LIFE_DAYS
    assert abs(g._recency_weight(half, newest) - 0.5) < 1e-9
    assert g._recency_weight(None, newest) == 1.0   # missing date -> neutral


def test_mc_best_third_band_and_determinism():
    shots = [(d, 1.0) for d in [100, 150, 200, 250, 260, 270, 280]]
    a = g._mc_best_third(shots, seed=123)
    b = g._mc_best_third(shots, seed=123)
    assert a == b                       # deterministic for a given seed
    med, lo, hi, n = a
    assert n == 7
    assert lo <= med <= hi              # median inside the band
    assert med > 200                    # best-third skews toward the long shots
    # single shot -> point estimate, no spread
    assert g._mc_best_third([(265, 1.0)], seed=1) == (265, 265, 265, 1)
    assert g._mc_best_third([], seed=1) is None


def test_gapping_ladder_flags_overlap_and_gap():
    bag = [
        {"club": "Driver", "category": "Driver", "group": "Woods", "carry": 250,
         "total": 250, "total_lo": 242, "total_hi": 260, "confidence": "high"},
        {"club": "3 Wood", "category": "Wood", "group": "Woods", "carry": 225,
         "total": 225, "total_lo": 218, "total_hi": 232, "confidence": "medium"},
        {"club": "5 Iron", "category": "Iron", "group": "Irons", "carry": 180,
         "total": 180, "total_lo": 176, "total_hi": 184, "confidence": "high"},
        {"club": "6 Iron", "category": "Iron", "group": "Irons", "carry": 178,
         "total": 178, "total_lo": 172, "total_hi": 183, "confidence": "high"},
        {"club": "9 Iron", "category": "Iron", "group": "Irons", "carry": 140,
         "total": 140, "total_lo": 136, "total_hi": 144, "confidence": "low"},
    ]
    rows = g._gapping_ladder_rows(bag)
    by_club = {r["club"]: r for r in rows}
    assert "9 Iron" not in by_club
    assert "GAP" in by_club["3 Wood"]["flags"]
    assert "OVERLAP" in by_club["5 Iron"]["flags"]


def test_standout_shots_pick_best_worst_and_format_putt_feet():
    rows = [
        {"round_id": "r1", "hole_id": "4", "shot_num": "2", "club": "Putter",
         "club_category": "Putter", "start_dist_to_pin_yd": "8.0",
         "end_dist_to_pin_yd": "0.0", "is_putt": "1", "is_tee": "0",
         "penalties": "0", "category_approx": "putting", "sg_shot_approx": "0.82"},
        {"round_id": "r1", "hole_id": "7", "shot_num": "2", "club": "7 Iron",
         "club_category": "Iron", "start_dist_to_pin_yd": "168",
         "end_dist_to_pin_yd": "1.3", "is_putt": "0", "is_tee": "0",
         "penalties": "0", "category_approx": "approach", "sg_shot_approx": "0.72"},
        {"round_id": "r1", "hole_id": "12", "shot_num": "1", "club": "Driver",
         "club_category": "Driver", "start_dist_to_pin_yd": "420",
         "end_dist_to_pin_yd": "0", "is_putt": "0", "is_tee": "1",
         "penalties": "1", "category_approx": "off_tee", "sg_shot_approx": "-2.10"},
        {"round_id": "r2", "hole_id": "1", "sg_shot_approx": ""},
    ]
    by_round = g._standout_shots_by_round(rows)
    assert by_round["r1"]["best"]["sg"] == 0.82
    assert "24 ft putt" in by_round["r1"]["best"]["desc"]
    assert by_round["r1"]["worst"]["sg"] == -2.10
    assert by_round["r1"]["worst"]["desc"] == "Hole 12 — tee shot, penalty"
    assert "r2" not in by_round


def test_dashboard_is_deterministic():
    # same data in -> identical Monte-Carlo output (no churn, reproducible)
    d1 = g.compute(REPO)
    d2 = g.compute(REPO)
    for a, b in zip(d1["dispersion"], d2["dispersion"]):
        assert (a["total"], a["total_lo"], a["total_hi"], a["carry"]) == \
               (b["total"], b["total_lo"], b["total_hi"], b["carry"])


# ------------------------------------------------- against the real data store
def _compute():
    return g.compute(REPO)


def test_index_is_official_ghin():
    d = _compute()
    profile = g._read_json(os.path.join(REPO, "ghin_profile.json"), {})
    official = g._f(profile.get("handicap_index"))
    if official is not None:
        assert d["player"]["index"] == official, "dashboard index must equal official GHIN"


def test_bag_strictly_descending():
    d = _compute()
    prev = None
    for b in d["bag"]:
        assert b["suggested"] is not None
        if prev is not None:
            assert b["suggested"] < prev, f'{b["club"]} breaks descending order'
        prev = b["suggested"]


def test_club_chart_rounded_to_5():
    d = _compute()
    for c in d["dispersion"]:
        for k in ("total", "carry", "carry_sd", "lateral_sd"):
            v = c[k]
            assert v is None or v % 5 == 0, f'{c["club"]}.{k}={v} not a multiple of 5'


def test_club_map_remaps_wedges():
    # club_map.json renames 54->56 and 58->60 at ingest; the old names must be gone
    d = _compute()
    names = [c["club"] for c in d["dispersion"]] + [b["club"] for b in d["bag"]]
    assert "54 Wedge" not in names and "58 Wedge" not in names
    assert "56 Wedge" in [b["club"] for b in d["bag"]]
    assert "60 Wedge" in [b["club"] for b in d["bag"]]


def test_bag_targets_come_from_bag_csv():
    # bag.csv is the source of truth: descending Driver 250 -> 60 Wedge 80, 5i 170
    d = _compute()
    tg = {b["club"]: b["target"] for b in d["bag"]}
    assert tg["Driver"] == 250 and tg["60 Wedge"] == 80 and tg["5 Iron"] == 170
    assert len(d["bag_specs"]) >= 10   # spec card populated from bag.csv


def test_aim_excludes_tee_shots():
    # Driver is hit off the tee, so it must NOT appear in aim-by-club (pin-line
    # offset off the tee is a dogleg artifact, not aim).
    d = _compute()
    assert "Driver" not in [a["club"] for a in d["aim"]]


def test_nine_hole_round_flagged_not_in_index():
    d = _compute()
    nine = [p for p in d["posted"] if p.get("holes") == 9]
    if nine:
        # the 9-hole round exists in posted scores but its differential is not the
        # index (index comes from official GHIN / 18-hole differentials)
        assert d["player"]["index"] is not None


def test_build_does_not_crash_on_empty_store(tmpdirless="."):
    # compute() on a directory with no data files must return a dict, not crash
    empty = os.path.join(HERE, "_does_not_exist_store")
    d = g.compute(empty)
    assert isinstance(d, dict)
    assert d["meta"]["n_rounds"] == 0


def test_distance_reconcile_and_monotonic_suppression():
    # Reconcile-vs-Arccos: every club carries the Arccos Smart Distance as the primary
    # reference, and our best-third is suppressed when it's low-sample or would create a
    # VISIBLE inversion (the 9-iron-reads-185 bug). Shown best-thirds never invert.
    d = _compute()
    disp = d["dispersion"]
    assert disp, "expected club dispersion rows"
    assert all("arccos" in c for c in disp), "Arccos reference missing on a club"
    assert all("best_third_suppressed" in c for c in disp), "suppression flag missing"
    prev = None
    for c in disp:  # displayed longest -> shortest
        if c.get("best_third_suppressed") or c.get("total") is None:
            continue
        if prev is not None:
            assert c["total"] <= prev, (
                f'{c["club"]} best-third {c["total"]} exceeds {prev} shown above it')
        prev = c["total"]
    # Cole's thin-sample irons must be suppressed (best-third hidden, Arccos still shown)
    assert any(c.get("best_third_suppressed") for c in disp), "expected some suppression"


# --------------------------------------------------------------------- runner
def _run():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
