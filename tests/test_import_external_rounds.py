#!/usr/bin/env python3
"""
Tests for import_external_rounds.py — the multi-source round merge/dedup/append layer,
plus gen_tracker's blank-SG handling for merged (non-Arccos) rounds.

Dependency-free: runs with plain `python`, no pytest needed (though `pytest tests/` works):

    python tests/test_import_external_rounds.py

The contract these guard (see docs/plans/2026-07-12-external-rounds-append-merge-foundation.md):
  1. no source files  -> merge is a byte-identical no-op (the additive guarantee)
  2. same (date, course) as Arccos -> dropped (Arccos authoritative), no double-count
  3. distinct round    -> appended, round_id namespaced, holes/shots grouped under it
  4. course-name variants ("Foo GC" vs "Foo Golf Club") dedup to one
  5. merged rounds carry BLANK strokes-gained, never 0
  6. running twice is idempotent
  7. malformed source CSV is skipped, never fatal; canonical files stay valid
  8. launch_monitor.csv (practice domain) is untouched
  9. gen_tracker counts a merged round in totals but excludes its blank SG from means
"""
import csv
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "dashboard"))
import import_external_rounds as m  # noqa: E402


# --------------------------------------------------------------------- helpers
def _write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _arccos_round(**kw):
    r = {c: "" for c in m.CANON_ROUND_COLS}
    r["source"] = "arccos"
    r.update(kw)
    return r


def _write_store(tmp, arccos_rounds, **source_files):
    """arccos_rounds -> rounds_summary.csv (canonical header incl `source`).
    source_files: name='garmin_rounds' etc -> (cols, rows)."""
    _write_csv(os.path.join(tmp, "rounds_summary.csv"), m.CANON_ROUND_COLS, arccos_rounds)
    # empty canonical holes/shots so _append targets a real header
    _write_csv(os.path.join(tmp, "holes.csv"), m.CANON_HOLE_COLS, [])
    _write_csv(os.path.join(tmp, "shots.csv"), m.CANON_SHOT_COLS, [])
    for name, (cols, rows) in source_files.items():
        _write_csv(os.path.join(tmp, f"{name}.csv"), cols, rows)


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


GARMIN_R = ["round_id", "date", "course", "holes", "score", "playerHandicap",
            "putts", "penalties"]
GARMIN_H = ["round_id", "hole", "par", "strokes", "putts", "penalties",
            "fairwayShotOutcome"]
GARMIN_S = ["round_id", "hole", "shotOrder", "clubId", "distance_yds", "start_lie"]
SS_R = ["round_id", "date", "course", "score", "score_to_par", "handicap"]


# --------------------------------------------------------------------- tests
def test_norm_course_strips_generic_suffixes():
    assert m._norm_course("WindRose GC") == "windrose"
    assert m._norm_course("WindRose Golf Club") == "windrose"
    assert m._norm_course("Pebble Beach Golf Links") == "pebble beach"
    assert m._norm_course("The Old Course") == "old"  # 'the' + 'course' stripped
    assert m._norm_course("") == ""


def test_norm_course_generic_names_do_not_collide():
    """Review blocker: all-generic names must NOT normalize to '' — that collided
    'The Links' with 'The Country Club' (and blank courses) into one dedup key and
    silently dropped real rounds."""
    assert m._norm_course("The Links") != ""
    assert m._norm_course("The Country Club") != ""
    assert m._norm_course("The Links") != m._norm_course("The Country Club")
    assert m._norm_course("Golf Course") != m._norm_course("The National")
    with tempfile.TemporaryDirectory() as tmp:
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="The Links", score="80")],
            garmin_rounds=(GARMIN_R, [{"round_id": "900", "date": "2026-06-01",
                                       "course": "The Country Club", "score": "88"}]),
        )
        summary = m.merge_store(tmp)
        assert summary["added_rounds"] == 1, "distinct generic-name course must merge, not dedup"


def test_old_header_store_upgraded_with_source():
    """Review finding: appending to a pre-`source` (old-schema) store must not silently
    drop provenance. The store is upgraded in place: source column inserted after
    round_id, legacy rows tagged arccos, appended rows keep their tag."""
    with tempfile.TemporaryDirectory() as tmp:
        old_cols = [c for c in m.CANON_ROUND_COLS if c != "source"]
        _write_csv(os.path.join(tmp, "rounds_summary.csv"), old_cols,
                   [{"round_id": "1", "date": "2026-06-01", "course": "Foo GC",
                     "score": "80"}])
        _write_csv(os.path.join(tmp, "holes.csv"),
                   [c for c in m.CANON_HOLE_COLS if c != "source"], [])
        _write_csv(os.path.join(tmp, "shots.csv"),
                   [c for c in m.CANON_SHOT_COLS if c != "source"], [])
        _write_csv(os.path.join(tmp, "garmin_rounds.csv"), GARMIN_R,
                   [{"round_id": "900", "date": "2026-06-08", "course": "Bar CC",
                     "score": "88"}])
        m.merge_store(tmp)
        with open(os.path.join(tmp, "rounds_summary.csv"), newline="") as f:
            r = csv.reader(f)
            header = next(r)
        assert header[:3] == ["round_id", "source", "date"], f"bad upgraded header: {header[:3]}"
        rows = _read(os.path.join(tmp, "rounds_summary.csv"))
        by_id = {x["round_id"]: x for x in rows}
        assert by_id["1"]["source"] == "arccos", "legacy row must be tagged arccos"
        assert by_id["garmin:900"]["source"] == "garmin", "appended row must keep its source"


def test_partial_par_not_backfilled():
    """Review finding: 8 of 9 pars known must NOT backfill an understated par (score_to_par
    would inflate). Holes count still backfills."""
    with tempfile.TemporaryDirectory() as tmp:
        holes = [{"round_id": "900", "hole": str(i), "par": "4", "strokes": "5"}
                 for i in range(1, 9)] + [{"round_id": "900", "hole": "9", "par": "",
                                           "strokes": "5"}]
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="Foo GC", score="80")],
            garmin_rounds=(GARMIN_R, [{"round_id": "900", "date": "2026-06-08",
                                       "course": "Bar CC", "score": "88"}]),
            garmin_holes=(GARMIN_H, holes),
        )
        m.merge_store(tmp)
        g = [r for r in _read(os.path.join(tmp, "rounds_summary.csv"))
             if r["source"] == "garmin"][0]
        assert g["par"] == "", f"partial par must stay blank, got {g['par']!r}"
        assert g["holes"] == "9"


def test_same_day_different_course_kept_but_warned():
    """Review finding: cross-source course-name divergence can evade dedup. The round is
    kept (may be a legit second course) but a WARN is emitted for audit."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="TPC Sawgrass", score="80")],
            garmin_rounds=(GARMIN_R, [{"round_id": "900", "date": "2026-06-01",
                                       "course": "Stadium Course at TPC Sawgrass",
                                       "score": "80"}]),
        )
        summary = m.merge_store(tmp)
        assert summary["added_rounds"] == 1
        assert any("possible double-count" in w for w in summary["warnings"])


def test_no_source_files_is_a_noop():
    """The additive guarantee: with no external sources present, the merge changes nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_store(tmp, [_arccos_round(round_id="1", date="2026-06-01", course="Foo GC",
                                         score="80", sg_total_arccos="1.5")])
        before = open(os.path.join(tmp, "rounds_summary.csv"), "rb").read()
        summary = m.merge_store(tmp)
        after = open(os.path.join(tmp, "rounds_summary.csv"), "rb").read()
        assert before == after, "no-op merge must not touch rounds_summary.csv"
        assert summary["added_rounds"] == 0


def test_dedup_arccos_wins_same_date_course():
    with tempfile.TemporaryDirectory() as tmp:
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="Foo GC",
                           score="80", sg_total_arccos="1.5")],
            garmin_rounds=(GARMIN_R, [{"round_id": "900", "date": "2026-06-01",
                                       "course": "Foo Golf Club", "score": "80"}]),
        )
        summary = m.merge_store(tmp)
        rounds = _read(os.path.join(tmp, "rounds_summary.csv"))
        assert len(rounds) == 1, "same (date, course) as Arccos must not double-count"
        assert rounds[0]["source"] == "arccos"
        assert summary["added_rounds"] == 0
        assert any("dup of existing" in d for d in summary["dropped"])


def test_distinct_round_is_appended_and_namespaced():
    with tempfile.TemporaryDirectory() as tmp:
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="Foo GC", score="80",
                           sg_total_arccos="1.5")],
            garmin_rounds=(GARMIN_R, [{"round_id": "900", "date": "2026-06-08",
                                       "course": "Bar CC", "score": "88", "putts": "33"}]),
            garmin_holes=(GARMIN_H, [{"round_id": "900", "hole": "1", "par": "4",
                                      "strokes": "5", "putts": "2",
                                      "fairwayShotOutcome": "HIT"}]),
            garmin_shots=(GARMIN_S, [{"round_id": "900", "hole": "1", "shotOrder": "1",
                                      "clubId": "D", "distance_yds": "250"}]),
        )
        summary = m.merge_store(tmp)
        rounds = _read(os.path.join(tmp, "rounds_summary.csv"))
        assert len(rounds) == 2 and summary["added_rounds"] == 1
        g = [r for r in rounds if r["source"] == "garmin"][0]
        assert g["round_id"] == "garmin:900"
        assert g["score"] == "88" and g["putts"] == "33"
        holes = _read(os.path.join(tmp, "holes.csv"))
        shots = _read(os.path.join(tmp, "shots.csv"))
        assert [h["round_id"] for h in holes] == ["garmin:900"]
        assert holes[0]["date"] == "2026-06-08" and holes[0]["course"] == "Bar CC"
        assert holes[0]["fairway_hit"] == "1"
        assert [s["round_id"] for s in shots] == ["garmin:900"]
        # Garmin round summary has no total par nor holes count -> backfilled from the
        # single par-4 hole so the round carries par=4 and holes=1.
        assert g["par"] == "4" and g["holes"] == "1"


def test_merged_round_has_blank_sg_not_zero():
    with tempfile.TemporaryDirectory() as tmp:
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="Foo GC", score="80",
                           sg_total_arccos="1.5")],
            garmin_rounds=(GARMIN_R, [{"round_id": "900", "date": "2026-06-08",
                                       "course": "Bar CC", "score": "88"}]),
        )
        m.merge_store(tmp)
        g = [r for r in _read(os.path.join(tmp, "rounds_summary.csv"))
             if r["source"] == "garmin"][0]
        for k in ("sg_total_arccos", "sg_off_tee_arccos", "sg_putting_arccos",
                  "sg_total_broadie"):
            assert g[k] == "", f"{k} must be blank for a non-Arccos round, got {g[k]!r}"


def test_idempotent_second_run_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="Foo GC", score="80")],
            garmin_rounds=(GARMIN_R, [{"round_id": "900", "date": "2026-06-08",
                                       "course": "Bar CC", "score": "88"}]),
        )
        m.merge_store(tmp)
        after_first = open(os.path.join(tmp, "rounds_summary.csv"), "rb").read()
        summary2 = m.merge_store(tmp)
        after_second = open(os.path.join(tmp, "rounds_summary.csv"), "rb").read()
        assert after_first == after_second, "second run must be idempotent"
        assert summary2["added_rounds"] == 0


def test_malformed_source_is_skipped_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="Foo GC", score="80")],
            # junk rows: no date/course -> skipped
            garmin_rounds=(["round_id", "junk"], [{"round_id": "x", "junk": "???"}]),
        )
        summary = m.merge_store(tmp)  # must not raise
        rounds = _read(os.path.join(tmp, "rounds_summary.csv"))
        assert len(rounds) == 1 and summary["added_rounds"] == 0
        assert any("missing date/course" in d for d in summary["dropped"])


def test_implausible_date_is_skipped():
    """A future-dated (or unparseable) source round must not enter the canonical files —
    it would skew recency-weighted stats and can crash the Monte-Carlo render."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="Foo GC", score="80")],
            garmin_rounds=(GARMIN_R, [
                {"round_id": "900", "date": "2099-01-01", "course": "Future GC", "score": "88"},
                {"round_id": "901", "date": "not-a-date", "course": "Junk GC", "score": "88"},
            ]),
        )
        summary = m.merge_store(tmp)
        assert summary["added_rounds"] == 0
        assert sum("implausible date" in d for d in summary["dropped"]) == 2
        assert len(_read(os.path.join(tmp, "rounds_summary.csv"))) == 1


def test_launch_monitor_is_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="Foo GC", score="80")],
            garmin_rounds=(GARMIN_R, [{"round_id": "900", "date": "2026-06-08",
                                       "course": "Bar CC", "score": "88"}]),
        )
        lm_path = os.path.join(tmp, "launch_monitor.csv")
        _write_csv(lm_path, ["date", "club", "carry_yd"],
                   [{"date": "2026-06-05", "club": "Driver", "carry_yd": "250"}])
        before = open(lm_path, "rb").read()
        m.merge_store(tmp)
        assert open(lm_path, "rb").read() == before, "practice domain must be untouched"


def test_shotscope_native_sg_lands_on_shot_not_round():
    with tempfile.TemporaryDirectory() as tmp:
        SS_H = ["round_id", "holeNumber", "par", "score", "putts"]
        SS_S = ["round_id", "holeNumber", "shotNumber", "clubName", "lie",
                "distanceToPin", "strokesGained"]
        _write_store(
            tmp,
            [_arccos_round(round_id="1", date="2026-06-01", course="Foo GC", score="80")],
            shotscope_rounds=(SS_R, [{"round_id": "77", "date": "2026-06-09",
                                      "course": "Sea Links", "score": "85",
                                      "score_to_par": "13", "handicap": "12"}]),
            shotscope_holes=(SS_H, [{"round_id": "77", "holeNumber": str(i), "par": "4",
                                     "score": "5", "putts": "2"} for i in range(1, 10)]),
            shotscope_shots=(SS_S, [{"round_id": "77", "holeNumber": "1", "shotNumber": "3",
                                     "clubName": "7 Iron", "distanceToPin": "150",
                                     "strokesGained": "0.4"}]),
        )
        m.merge_store(tmp)
        r = [x for x in _read(os.path.join(tmp, "rounds_summary.csv"))
             if x["source"] == "shotscope"][0]
        assert r["sg_total_arccos"] == "" and r["score_to_par"] == "13"
        # Shot Scope summary has no holes count -> backfilled from 9 hole rows, so it
        # survives statcard's holes>=9 course-standings filter (par backfilled too).
        assert r["holes"] == "9" and r["par"] == "36"
        s = _read(os.path.join(tmp, "shots.csv"))[0]
        assert s["sg_shot_approx"] == "0.4" and s["club"] == "7 Iron"


# --------------------------------------------------- gen_tracker integration (blank SG)
def _gt():
    import gen_tracker as g
    return g


def test_gen_tracker_counts_merged_round_but_excludes_blank_sg_from_means():
    import statistics
    g = _gt()
    with tempfile.TemporaryDirectory() as tmp:
        a1 = _arccos_round(round_id="1", date="2026-06-01", course="Foo GC", par="72",
                           score="78", score_to_par="6", sg_off_tee_arccos="1.0",
                           sg_total_arccos="2.0", sg_approach_arccos="0.5",
                           sg_short_arccos="0.3", sg_putting_arccos="0.2")
        a2 = _arccos_round(round_id="2", date="2026-06-04", course="Foo GC", par="72",
                           score="82", score_to_par="10", sg_off_tee_arccos="-1.0",
                           sg_total_arccos="-1.0", sg_approach_arccos="-0.2",
                           sg_short_arccos="0.1", sg_putting_arccos="0.1")
        _write_store(
            tmp, [a1, a2],
            garmin_rounds=(GARMIN_R, [{"round_id": "900", "date": "2026-06-08",
                                       "course": "Bar CC", "score": "90", "putts": "34"}]),
        )
        before = g.compute(tmp)
        m.merge_store(tmp)
        after = g.compute(tmp)

        # the merged round is COUNTED
        assert len(after["rounds"]) == len(before["rounds"]) + 1
        garmin = [r for r in after["rounds"] if str(r["round_id"]).startswith("garmin:")]
        assert len(garmin) == 1
        assert garmin[0]["sg_total"] is None, "merged round must carry blank SG, not 0"

        # SG-category means are UNCHANGED by the blank-SG round (excluded from the average)
        def _macro(compd, cat):
            return next(x["macro"] for x in compd["coaching"]["sg_compare"]
                        if x["cat"] == cat)
        for cat in ("Off the tee", "Approach", "Short game", "Putting"):
            assert _macro(after, cat) == _macro(before, cat), (
                f"{cat} macro moved when a blank-SG round was merged")
        # sanity: the Off-the-tee macro is really the mean of the two Arccos rounds
        assert _macro(after, "Off the tee") == round(statistics.fmean([1.0, -1.0]), 1)


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
            import traceback
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
