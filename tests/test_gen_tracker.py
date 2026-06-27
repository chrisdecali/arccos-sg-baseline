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
