# Arccos Strokes-Gained Baseline

Comprehensive personal golf data pulled from the **unofficial, reverse-engineered
Arccos Golf API** (owner's own account). Not affiliated with Arccos Golf LLC.
Rebuilt by an idempotent weekly pipeline (`pull_arccos.py`): caches every reachable
endpoint, then derives these public outputs. Shot GPS coordinates ARE included
(shots.csv/holes.csv + maps/); only identity (name, email, DOB, GHIN number) is
kept out.

Owner is a ~12 handicap; **biggest leak = chipping / short game** — the data confirms
it (career chip-save 0/7, sand-save 0/3, SG short −6.1). Short-game columns are the
focus.

## Files

| File | Grain | What |
|---|---|---|
| `rounds_summary.csv` | round | scoring, pace, GIR/FW/scramble, putting, driving, **real Arccos strokes-gained + an independent Broadie reconstruction**, Arccos category handicaps |
| `holes.csv` | hole | **real par** (from Arccos), net score, GIR/FW/putts, scramble, proximity, per-hole SG |
| `shots.csv` | shot | club, shot distance, **distance-to-pin (start/end)**, **start/end GPS**, approx lie, category, per-shot SG |
| `holes.csv` (geo) / `maps/` | — | per-hole **pin GPS**; `maps/round_*.kml` + `.geojson` + `shots_geo.csv` for Google Earth → PDF |
| `clubs.csv` | club | **make/model**, smart distance, terrain splits (tee/fairway/rough/sand), GIR%, **dispersion** |
| `handicap_history.csv` | round | Arccos category handicaps over time |
| `career_stats.json` | aggregate | **Arccos career stats** — SG, chip/sand save %, GIR, fairways, putting, score-by-par, caddie insights |
| `player_profile.json` | — | redacted profile, bag, ball, home course, subscription |
| `arccos_sg_baseline.xlsx` | — | tabs: Rounds, Holes, Shots, Clubs, Handicap History, **Career Stats**, **Baseline Summary** (short-game highlighted) |

## Strokes-gained: two independent sources

1. **`sg_*_arccos`** — Arccos's **real** strokes-gained, from
   `GET /sga/getDashboardAnalysis` with `goalHcp=0` (vs scratch). Authoritative.
2. **`sg_*_broadie`** — an **independent reconstruction** I compute from each shot's
   GPS distance-to-pin vs the published Broadie PGA-Tour baseline
   (`SG = E(start) − E(end) − 1 − penalties`). It exists as a cross-check and to
   give **per-shot** SG (`shots.csv`), which Arccos doesn't expose.

They agree closely on round 1 (Arccos total −23.7 vs Broadie −22.8; approach −14.6
vs −14.2). The Broadie putting/short figures are noisier (GPS lie/putt-distance is
approximate) — trust `sg_*_arccos` for category totals, use `sg_*_broadie` for
shot-level detail. SG is **vs scratch**, so large negatives are normal for a
mid-handicap.

## Column notes

- Booleans are `1`/`0`; blank = not returned by the API.
- **`holes.par`** is now the **real** Arccos par (`par_source=arccos`); `inferred`
  only appears if a round's dashboard was unavailable (GPS-length fallback).
- **`*_native`** = verbatim Arccos hole flags. `updown_*` / `sand_*` native flags
  were unpopulated (0) at first pull — the derived `scramble_*` columns and
  `career_stats` chip/sand rates are the real short-game measures.
- **`scramble_pct`** (rounds/holes) = missed GIR + holed ≤ par. `career_stats`
  additionally has Arccos's own **chip save %**, **chip down %**, **chip error
  rate**, **sand save %** (from `tourAnalyticsSummary`).
- **`avg_approach_proximity_yd`** — distance-to-pin of the last non-putt shot.
- **`*_hcp`** — Arccos' proprietary per-category handicap metric (negative scale,
  NOT USGA; approach/chip floor at −30).
- **`clubs.csv`** names come from the paired sensors (make/model real); the generic
  type label (e.g. "Wedge") plus distance disambiguates lofts. Never fabricated.

## Reproduce

The full pipeline is `pull_arccos.py` (stdlib only + `openpyxl` for the xlsx):

```bash
pip install openpyxl
# your own short-lived bearer token + user id from the Arccos web dashboard
# (DevTools -> Network -> any api.arccosgolf.com GET -> Authorization value + URL id)
echo '{"bearer_token":"...","user_id":"..."}' > ~/.arccos_creds.json && chmod 600 ~/.arccos_creds.json
python3 pull_arccos.py            # --fetch (cache every endpoint) then --build (these files)
python3 pull_arccos.py --discover # dump raw endpoint JSON structure
```

Writes outputs next to itself; the raw cache (`_cache_raw/`, contains GPS + PII) is
gitignored. Unofficial API — your own account only, no aggressive polling.

## GHIN — official USGA handicap

`ghin_scores.csv` / `ghin_handicap_history.csv` / `ghin_profile.json` = your official
USGA record, pulled by `pull_ghin.py` (read-only; paste a ~12h GHIN Bearer from
GHIN.com DevTools — never your password). This consolidates **every posted round**:
Arccos auto-posts **plus** the 18Birdies rounds posted manually. It gives the real
**WHS Handicap Index** (vs Arccos's proprietary `-20.x` scale).

Current index: **NH** — Not enough scores yet (WHS establishes after 54 holes / 3
scores; once enough post, the index appears here and `ghin_handicap_history.csv`
fills in). Refresh anytime with `python3 pull_ghin.py`. Outputs are redacted —
name, email, and GHIN number are stripped.

## 18Birdies → GHIN

Getting 18Birdies-only rounds onto your USGA handicap: 18Birdies has no GHIN
integration, so use `eighteenbirdies_to_ghin.py` to turn your 18Birdies data
export into a GHIN-entry worksheet (dedupes against GHIN, fills rating/slope),
then post the few missing rounds in the GHIN app. Read-only — never posts, never
takes a password. Details + the optional auto-post recon: `docs/18birdies-to-ghin.md`.

Raw 18Birdies rounds (native stats — fairways/GIR/putts/scoring distribution) live in
`18birdies_rounds.csv`, produced by `pull_18birdies.py` from the same export. (18Birdies
has no shot/GPS data and its strokes-gained is premium-gated, so neither is included.)

## Provenance

Source: unofficial reverse-engineered Arccos API (`api.arccosgolf.com`), pulled by
`pull_arccos.py`. Endpoints found by live recon: rounds, round detail (shots),
courses, handicaps, `/v4` & `/v6` clubs, `/sga/getDashboardAnalysis`,
`tourAnalyticsSummary`, `/sga/playerProfile`, achievements, analytics. SG baseline:
Broadie, *Every Shot Counts* Table 9. May break if Arccos changes their backend.
