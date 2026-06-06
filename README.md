# Arccos Strokes-Gained Baseline

Comprehensive personal golf data pulled from the **unofficial, reverse-engineered
Arccos Golf API** (owner's own account). Not affiliated with Arccos Golf LLC.
Rebuilt by an idempotent weekly pipeline (`pull_arccos.py`): fetches every reachable
endpoint into a local cache, then derives these public outputs. GPS coordinates,
name, email and DOB are never included.

Owner is a ~12 handicap; **biggest leak = chipping / short game**, so short-game,
scrambling and proximity columns matter most.

## Files

| File | Grain | What |
|---|---|---|
| `rounds_summary.csv` | one row / round | scoring, GIR/FW/scramble, putting, driving, **reconstructed strokes-gained by category**, Arccos category handicaps |
| `holes.csv` | one row / hole | calibrated par, score-to-par, GIR/FW/putts, scramble, proximity, per-hole SG |
| `shots.csv` | one row / shot | club, shot distance, **distance-to-pin (start/end)**, approx lie, category, **per-shot SG** (no GPS) |
| `clubs.csv` | one row / club | smart distance, **terrain splits** (tee/fairway/rough/sand), GIR%, **dispersion** (range), usage |
| `handicap_history.csv` | one row / round | Arccos category handicaps over time |
| `player_profile.json` | — | redacted profile, bag, preferred ball, home course, subscription |
| `arccos_sg_baseline.xlsx` | — | all of the above as tabs + **Baseline Summary** (short-game rows highlighted) + glossary |

## How strokes-gained is produced (important)

Arccos's official SG endpoint (`/v2/sga/shots`) returns **401** for dashboard
tokens, so SG here is **RECONSTRUCTED**, not taken from Arccos:

1. Every shot has start + end GPS and the hole's pin location → I compute
   **distance-to-pin** for the start and end of each shot (haversine).
2. Expected strokes-to-hole-out come from the published **Broadie PGA-Tour
   baseline** (Table 9, *Every Shot Counts*).
3. `SG(shot) = ExpStrokes(start_lie, start_dist) − ExpStrokes(end_lie, end_dist) − 1 − penalties`
   (end term = 0 when holed).
4. Shots roll up to **off-tee / approach / short-game / putting**.

SG is **vs a scratch/tour benchmark** — large negatives are expected for a
mid-handicap (round 1: SG total ≈ −23 on a 96). Treat as **indicative**: per-shot
**lie is approximate** (the API only labels tee/green reliably; fairway-vs-rough is
inferred from the hole's fairway flag), and putt distances come from GPS so short
putts are noisy. Columns carry `_approx` to flag this.

## Column notes

- Booleans are `1`/`0`; blank = not returned by the API.
- **`*_native`** = verbatim Arccos fields. `updown_*` / `sand_*` native flags were
  **unpopulated (all 0)** at first pull — 0 means "no data", not "0%". Scrambling
  below is the derived replacement.
- **`par_calibrated`** — per-hole par is NOT in the API. Inferred from GPS hole
  length (≤245yd=3, ≤470=4, else 5) then nudged so the 18 holes sum to the real
  course par (fixes dogleg misreads).
- **`scramble_*`** — missed GIR + holed out ≤ `par_calibrated`. `scramble_pct` =
  saves / chances.
- **`approach_proximity_yd`** — end distance-to-pin of the **last non-putt shot**
  (the approach on GIR holes; the chip on scramble holes).
- **`drive_yd` / `avg_drive_yd`** — tee-shot carry+roll (start→end) on par 4/5.
- **`*_hcp`** (`user_hcp`, `drive_hcp`, `approach_hcp`, `chip_hcp`, `sand_hcp`,
  `putt_hcp`) — Arccos' **proprietary** per-category handicap-style metrics.
  Negative scale, **NOT** USGA index; approach/chip clamp at −30. Kept as-is.
- **`clubs.csv` names** — mapped from shot data (clubId→clubType). Clubs not yet
  hit show `club<id>`; they resolve as more rounds accumulate. Never fabricated.

## Provenance

Source: unofficial reverse-engineered Arccos API, pulled by `pull_arccos.py`
(custom stdlib client; endpoints found by live recon). SG baseline: Broadie,
*Every Shot Counts* / "Assessing Golfer Performance on the PGA TOUR" Table 9.
May break if Arccos changes their backend.
