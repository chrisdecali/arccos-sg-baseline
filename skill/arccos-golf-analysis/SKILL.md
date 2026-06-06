---
name: arccos-golf-analysis
description: Analyze the player's Arccos golf data — strokes-gained, short game, scrambling, putting, club distances, where to improve. Use whenever the user asks about their golf game, stats, rounds, strokes gained, chipping/short game, putting, driving, or club gapping. Data is the public repo Colewinds/arccos-sg-baseline.
---

# Arccos Golf Analysis

The player's Arccos data lives in the **public** repo `Colewinds/arccos-sg-baseline`,
rebuilt weekly by `pull_arccos.py`. Reading needs **no credentials** — fetch the raw
files and analyze.

## Get the data

Base URL: `https://raw.githubusercontent.com/Colewinds/arccos-sg-baseline/main/`

Fetch (WebFetch, or in a sandbox `curl`/`requests`):
1. **`career_stats.json`** — START HERE. Aggregate strokes-gained + chip/sand save
   rates + putting + score-by-par + Arccos caddie insights.
2. `rounds_summary.csv` — one row per round.
3. `holes.csv`, `shots.csv` — per-hole / per-shot detail (shots has distance-to-pin + per-shot SG).
4. `clubs.csv` — per-club smart distance, terrain splits, dispersion, GIR%.
5. `handicap_history.csv`, `player_profile.json`.
6. `README.md` — full column glossary (read if a column is unclear).

## Player context (drives every analysis)

~12 handicap. **Biggest leak = chipping / short game.** Lead with short-game
findings: scramble %, chip save %, sand save %, SG short game, proximity, 3-putts.
Driving is a relative strength — don't over-index on it.

## Reading the data correctly (avoid wrong conclusions)

- **Strokes gained**: use `sg_*_arccos` (Arccos's real SG vs scratch). `sg_*_broadie`
  is an independent per-shot reconstruction (cross-check; its putting figure is
  noisier — prefer `_arccos` for category totals, `_broadie` only for shot-level).
- **Short game truth** = `career_stats.json` → `key_rates` (chip save %, chip down %,
  chip error rate, sand save %) and `career_by_category.chip` / `.sand`. These are
  populated and reliable.
- **Ignore `*_native` up-and-down / sand-save hole flags** in holes.csv — they were
  unpopulated (0) at pull time. Use the career_stats chip/sand rates instead, or the
  derived `scramble_*` columns.
- **`*_hcp`** (drive/approach/chip/sand/putt) = Arccos' proprietary NEGATIVE skill
  scale, NOT a USGA index. More negative = weaker. Approach/chip floor at −30.
- `holes.par` is real Arccos par (`par_source=arccos`).
- Sample size is small early (grows weekly) — caveat conclusions when n is low.

## Refreshing the data (optional, needs a token)

To pull NEW rounds, run the bundled `pull_arccos.py` (one dir up, repo root):
1. Get a fresh bearer token: dashboard.arccosgolf.com → DevTools → Network → any
   `api.arccosgolf.com` GET → copy the `Authorization` value + the user id in the URL.
2. `echo '{"bearer_token":"...","user_id":"..."}' > ~/.arccos_creds.json`
3. `pip install openpyxl && python3 pull_arccos.py` then commit/push.

Caveat: token extraction requires the user's browser (a cloud sandbox can't reach
their Arccos session), and the fetch needs outbound network to `api.arccosgolf.com`.
Reading the already-published data has neither requirement.
