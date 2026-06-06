# Arccos Strokes-Gained Baseline

Personal golf performance data pulled from the **unofficial, reverse-engineered
Arccos Golf API** (owner's own account). Not affiliated with Arccos Golf LLC.
Rebuilt by an idempotent weekly pipeline; files only ever gain rows (deduped on
`round_id`).

| File | What |
|---|---|
| `rounds_summary.csv` | one row per round |
| `holes.csv` | one row per hole per round |
| `arccos_sg_baseline.xlsx` | Rounds + Holes tabs + **Baseline Summary** (averages, short-game rows highlighted, provenance) |

## Reading the columns

- Boolean stats are `1`/`0` (blank = not returned by the API).
- **`*_native` columns** are verbatim Arccos fields. `updown_*` / `sand_save_*`
  native flags were **unpopulated (all 0)** as of first pull — treat 0 as
  "no data", not "0%".
- **`*_inferred` columns are DERIVED, not API data**:
  - `par_inferred` — from GPS hole length (first shot → pin, straight line):
    ≤245yd par 3, ≤470 par 4, else par 5. Doglegs can misread (e.g. inferred
    course par 70 vs actual 72 on round 1).
  - `scramble_*_inferred` — missed GIR (native flag) + holed out ≤ `par_inferred`.
  - `fairway_pct_inferred` — fairway hits / holes with `par_inferred` ≥ 4.
- **`drive_hcp` / `approach_hcp` / `chip_hcp` / `sand_hcp` / `putt_hcp`** are
  Arccos per-category handicap-style metrics returned per round. Semantics
  unverified; appears clamped at ±30. They are an **SG proxy, NOT true strokes
  gained** (the real SG endpoint is permission-gated).
- GPS coordinates and round notes are deliberately excluded.

## Owner context

~12 handicap; priority leak = **chipping / short game** — watch
`scramble_pct_inferred`, `chip_hcp`, `sand_hcp`, and putts-after-miss patterns.
