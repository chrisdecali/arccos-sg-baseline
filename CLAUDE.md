# CLAUDE.md — arccos-sg-baseline

Brief for Claude Code working in this repo. Read this first. It captures the data
layout, how to build the dashboard, the analysis conventions we've settled on, and the
player context so recommendations stay calibrated.

## What this repo is

A golf shot-data store (pulled from Arccos + GHIN + 18Birdies) that auto-regenerates
~weekly, plus a generator that turns it into a shareable analytics dashboard. Owner:
Chris Cole. Goal: lower scores for PGA Frisco (Oct 21–24, 2026) and steady handicap
improvement.

> **Repo reality (updated 2026-06-15).** This repo holds the **data store + pull
> scripts + the single-page dashboard generator** (`dashboard/gen_tracker.py`) + a
> read/analyze skill (`skill/arccos-golf-analysis/`). The **per-round render layer**
> (`gen_combined`, `gen_stats`, `gen_satellite`, `gen_gps_pdf`, `build_round_pages`)
> lives in the separate **`golf-reports`** repo, which runs locally as an MCP server —
> it is NOT in this repo. Per-round HTML+PDF already produced are committed under
> `reports/`.

## Data contract (read by column name; never assume position)

* `rounds_summary.csv` — one row per Arccos round. Columns include: `round_id, date,
  course, tee_name, tee_yards, slope, rating, holes, score, par, score_to_par,
  pace_of_play, putts, one_putts, three_putts, putts_per_gir, gir_hits, gir_pct,
  fairway_hits, fairway_chances, fairway_pct, scramble_chances, scramble_saves,
  scramble_pct, sand_chances_native, sand_saves_native, penalties, avg_drive_yd,
  longest_drive_yd, avg_approach_proximity_yd, sg_*_arccos, sg_*_broadie,
  user_hcp, drive_hcp, approach_hcp, chip_hcp, sand_hcp, putt_hcp`. Optional weather:
  `temp_f, wind_mph, wind_dir, weather` (auto-shown if present).
* `holes.csv` — `round_id, date, course, hole_id, par, par_source, shots, net_score,
  score_to_par, putts, penalties, gir, fairway_hit, fw_miss_left, fw_miss_right,
  *_native flags, hole_len_yd, drive_yd, approach_proximity_yd, pin_lat, pin_lng,
  scramble_chance, scramble_save, sg_hole_broadie`.
* `shots.csv` — `round_id, date, hole_id, shot_num, club, club_category,
  shot_distance_yd, start_dist_to_pin_yd, end_dist_to_pin_yd, start_lat, start_lng,
  end_lat, end_lng, start_alt, end_alt, is_half_swing, lie_approx, is_tee, is_putt,
  penalties, category_approx, sg_shot_approx`. GPS columns optional.
* `clubs.csv` — Arccos per-club aggregates (`club, club_category, club_make,
  club_model, smart_distance_yd, normalized_yd, tee/fairway/rough/sand_yd, longest_yd,
  range_low/high_yd, dispersion_yd, gir_pct, usage_count`). Noisy (see conventions).
* `dispersion.json` — empirical-Bayes per-club total-distance + lateral model (schema
  v1.0) with confidence. The golfsmart bridge artifact and the dashboard's dispersion
  source.
* `sga_bands.csv` — Arccos strokes-gained broken out by driving/approach/short/putting
  bands + chipping/sand accuracy with Arccos goals.
* `career_stats.json` — aggregate: `strokes_gained_arccos`, `key_rates` (chip/sand
  save %, 3-putt %, etc.), `career_by_category`, `score_analysis`, `caddie_insights`.
* `ghin_scores.csv` — official posted scores + differentials (source of truth for the
  WHS index trend). `handicap_history.csv` / `ghin_*.json` — supporting.

Conventions the code assumes: select a round by `round_id` (newest = last row);
booleans are `'1'`/`''`; distances in yards (putts shown in feet = ×3); a putt is
holed iff it's the max `shot_num` among that hole's putts.

## Build commands

```powershell
# Only hard dep is matplotlib; the HTML uses CDNs (Leaflet + Esri) client-side.
# A local venv at .venv already has it (see MIGRATION below).

# 1) Refresh data — MUST set GPS or every shot map goes blank (see warning below):
$env:GOLF_INCLUDE_GPS = "1"; python pull_arccos.py   # fetch+build; needs weather.py + openpyxl
python pull_ghin.py                                   # official GHIN scores/index

# 2) Build the dashboard + per-round review pages (-> docs/index.html + docs/rounds/):
python dashboard/gen_tracker.py .  docs/index.html

# Tests (dependency-free; guards index/distance/bag-order/geometry math):
python tests/test_gen_tracker.py     # the weekly refresh runs these before publishing

# 3) (optional) Shareable shot-map PDF per round — uses the golf-reports render layer:
#    python <golf-reports>/render/build_round_pages.py . docs/rounds --all
#    gen_tracker auto-links any *_shotmaps.pdf it finds in docs/rounds/.
```

> ⚠️ **GPS flag is load-bearing.** `pull_arccos.py` strips shot lat/lng from
> `shots.csv`/`holes.csv` UNLESS `GOLF_INCLUDE_GPS=1` (or `--include-gps`). Without
> it the dashboard map and every per-round review render "No GPS." Always set it for
> this repo (the README intends GPS to be public here; only identity is redacted).

Output to `docs/` so GitHub Pages serves the whole experience (dashboard +
`docs/rounds/<course>_<date>_review.html` per round). `gen_tracker.py` now emits the
per-round reviews itself (satellite shot map + hole-by-hole + SG), schema-matched to
this repo. Only the optional PDF uses the golf-reports render layer.

## Generator files (in dashboard/)

* `gen_tracker.py` — the single-page dashboard. Contains a pure `compute()` (CSVs/JSON
  in → dict out) plus all stat logic, then an HTML renderer. **This is the main
  artifact.** Renders: game plan, KPIs, WHS index projection (slider), cost of misses,
  approach & putting bands, scrambling, aim-by-club, dispersion explorer
  (Woods/Irons/Wedges filter), measured-vs-target bag, all-rounds satellite map,
  SG-by-round chart, trouble holes, posted scores. Keep `compute()` pure so a trend
  module can call it per round and stack.

## Analysis conventions (hard-won — keep these)

* **Distances: best-third strike, recency-weighted, Monte-Carlo validated.** Use the
  top-⅓ for every club/shot (on-course averages are poisoned by mishits/partials).
  Weight recent rounds more (`RECENCY_HALF_LIFE_DAYS`, exp decay). Then bootstrap
  (`_mc_best_third`, recency-weighted resampling, fixed seed → deterministic) to get a
  robust median + an 80% band, so thin samples read as uncertain (e.g. a 4-shot club
  shows a wide range). One estimate per club feeds BOTH the bag and dispersion.
* **Dispersion cleaning:** drop mishits with a carry floor = 0.8 × median carry, then
  drop lateral outliers via IQR (1.5×). Apply per selection.
* **The bag must stay strictly DESCENDING.** Never emit a yardage suggestion that puts
  a club out of order (a longer club carrying less than a shorter one). Only let a
  MEASURED carry override the target when it's high-confidence (n ≥ 8 AND within ~15%
  of target); otherwise hold target.
* **Label measured vs modeled, always.** Arccos SG categories = modeled-vs-scratch;
  shot SG = Broadie; make%/proximity = derived; peer carries, CHS, age curves =
  modeled estimates.
* **SG levers overlap ~35–40%.** Don't additively stack strokes-gained estimates;
  apply a ~0.62 efficiency factor to combined totals.
* **Handicap index = GHIN's official number, verbatim.** `gen_tracker.py` reads
  `handicap_index` from `ghin_profile.json` and shows that; the local `whs_index()`
  is only a fallback (before GHIN establishes an index) and for the projection
  what-if. Never override the official value with a local estimate — our WHS math
  can't perfectly mirror GHIN's small-sample adjustment, 9-hole pairing, or low-HI
  cap. **9-hole rounds are excluded from the index** (a lone nine posts a 9-hole
  differential that WHS pairs with another nine; using it raw crashes the index) and
  are flagged "9-hole / held" in posted scores. The projection is a labeled estimate
  on the most-recent-20 window, not the official number.
* **Equipment is low-leverage (~2 strokes max).** Short game + course management are
  ~75–80% of strokes over par. Spend recommendations accordingly.
* **`clubs.csv` is noisy.** Trust a club only at `usage_count ≥ ~5`; convert
  `smart_distance` (≈ total) → carry by category before comparing to carry baselines;
  otherwise fall back to the 18B set. Watch for Arccos mis-tags (phantom 4-iron,
  wedges logged by wrong loft).
* **Club distances are recomputed live from `shots.csv`, NOT from `dispersion.json`.**
  The dispersion explorer shows BOTH **Total** (recency-weighted Monte-Carlo best-third
  = the real measured distance ≈ what Arccos/you see, e.g. driver 270, with an 80%
  band) and **Carry** (Total × a category roll factor, e.g. 245 — modeled, see roll
  factor below). `dispersion.json` is now only read for `generated_at`/fallback. The
  numbers are also written to **`club_distances.csv`** (generated artifact: total,
  total_lo/hi, carry, carry SD, lateral SD, shots, confidence). The dispersion table
  shows *measured* numbers and may be non-monotonic; the measured-vs-target **bag** is
  the prescriptive, strictly-descending view.
* **Tee/aim bias** is measured to the pin line — note the dogleg caveat (you aim at
  the bend, not the flag, on doglegs). Don't surface an aim-change rec off < 6 clean
  shots. Bias ≥ ~5 yd (with enough n) → recommend aiming the opposite way; always add:
  confirm push-vs-aim on a launch monitor, don't aim into hazards.

## Player context (calibrate recs to this)

* Official GHIN index **14.1** (use the official number; realistic true level 14–17).
  Plays Augusta Pines blue (6,446 / 71.4/125); practices at WindRose; Frisco
  Oct 21–24, 2026.
* **Strength:** driving distance. Driver **total ~270** (best-third), launches it
  ~260–270 **carry** (modeled carry reads 245 until LM data); CHS ~108–110 → X-flex.
* **Primary leak:** short game — greenside CONTACT. 0/26 up-and-down, chips finishing
  ~27 ft from the hole (the chunk, not the putt). Then approach (low GIR) and 3-putts.
* **Bag:** Cobra DS-Adapt LS Dr/3W, DS-Adapt Max 5W, DS-Adapt 4H; Srixon ZX7 5i–PW
  (SteelFiber i110 S); Cleveland RTX6 GW/SW/LW (SteelFiber FC115 S); Tour Edge ZT-4
  putter. 18B carries: Dr 250, 3W 225, 5W 200, 4H 185, 5i 175, 6i 165, 7i 155, 8i 145,
  9i 135, PW 120, GW 105, SW 90, LW 80. Only data-backed change so far: 5-iron → ~165–170.
* **Carry is modeled (roll factor), launch monitor is the FUTURE source of truth.**
  Until LM data exists, carry = Total × `CARRY_FACTOR` (a roll-haircut guess; Arccos
  has no launch data). The player launches the driver ~260–270 carry (Arccos *total*
  best-third ~270, so little rollout — the modeled 245 carry under-reads it; that's
  expected and labeled "modeled" on the dashboard). **Plan:** he hits a launch monitor
  at **Tee Box in July**; that becomes its own authoritative carry source then. Do NOT
  hardcode LM carries before that — for now the dashboard is Arccos-only
  (recency-weighted, Monte-Carlo, top-⅓).
* **Medical:** small-fiber neuropathy, fibromyalgia, ataxia → graphite is required (not
  a compromise) and fitting must be launch-monitor/dispersion-driven — he can't feel
  spec changes. Never recommend steel or feel-based fitting.
* **Style:** concise, bulleted, specific numbers; expects red-teaming and
  measured-vs-modeled honesty.

## Gotchas

* Esri satellite tiles load client-side only (in the browser). Never fetch map tiles
  server-side in a sandbox — they're blocked (`host_not_allowed`). The dashboard HTML
  is the satellite path.
* GPS is optional everywhere — the map degrades to a note when `start_lat` is blank.
  Don't crash the build on missing GPS.
* Keep `compute()` pure (CSVs in → dict out) so a trend module can call it per round.
* Trend analyses (SG/proximity over time, per-hole patterns) need ~5+ rounds to be
  signal, not noise. With only 2 rounds today, caveat everything heavily.

## Local setup (Windows) & recurring refresh

* Built/run locally on Windows. A venv at `.venv` (gitignored) has matplotlib:
  `python -m venv .venv && .venv\Scripts\python -m pip install matplotlib`.
* Rebuild the dashboard: `.venv\Scripts\python dashboard\gen_tracker.py . docs\index.html`.
* **Recurring refresh:** add a remote routine in Claude Code — weekly (and/or on push
  to this repo) → pull fresh data, run `gen_tracker.py`, commit & push. GitHub Pages
  then serves the always-current dashboard at a bookmarkable URL, even with the laptop
  closed.
