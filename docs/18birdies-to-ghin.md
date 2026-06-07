# Getting 18Birdies rounds into GHIN

**Goal:** post rounds you logged in 18Birdies to your official USGA handicap (GHIN),
the way Arccos already auto-posts.

## TL;DR — the simplest, safest path

1. **18Birdies has no GHIN integration** (confirmed by 18Birdies help articles
   603/605/607) and no usable API. But it gives you a **password-free data export**.
2. **Arccos already auto-posts to GHIN**, so the only rounds that need posting are
   ones you logged in **18Birdies but not Arccos** — usually few.
3. Posting writes to your **real Handicap Index**, so the safe path is: generate a
   worksheet of the missing rounds, then **post them in the GHIN app yourself**.

### Steps

1. Download your 18Birdies data: <https://18birdies.com/download-account-data/> →
   `18Birdies_archive.json`.
2. Generate the worksheet (parses the file locally; no 18Birdies login):
   ```bash
   python3 eighteenbirdies_to_ghin.py 18Birdies_archive.json --home-course "WindRose"
   ```
   Optionally dedupe against GHIN and auto-fill course rating/slope (read-only):
   ```bash
   # GHIN.com -> DevTools -> Network -> any api2.ghin.com request -> copy the
   # "Authorization: Bearer ..." value (lasts ~12h). Never your password.
   python3 eighteenbirdies_to_ghin.py 18Birdies_archive.json \
       --ghin-bearer "eyJ..." --ghin-id 1234567 --default-tee "Blue" --home-course "WindRose"
   ```
   → `18birdies_rounds.csv` + `GHIN_ENTRY_WORKSHEET.md` (rounds marked ✅ are the
   ones not already on GHIN).
3. In the **GHIN app** → *Post Score* → pick the course + tee from the worksheet →
   enter the score (prefer **hole-by-hole** so GHIN applies Net-Double-Bogey) →
   set type **H** (home) or **A** (away). Do this only for the ✅ rows.

That's it. No credentials stored, nothing automated against your handicap.

## Why not auto-post from a script?

It's technically possible (see recon below) but **not worth the risk** for a few
rounds:

- It **writes to your official USGA index** — a wrong course rating/slope, wrong
  tee, or raw-vs-adjusted-gross mistake corrupts your real Handicap Index.
- **Double-posting**: Arccos (and your club) already post; an automated 18Birdies
  poster must dedupe perfectly or it inflates your index.
- Automated posting via the consumer GHIN API is **outside GHIN's ToS**, and its
  auth (Firebase/AES tokens) rotates, so a script breaks unpredictably.

The manual GHIN-app post for a handful of rounds is faster and can't silently
corrupt anything.

## Optional future phase — assisted auto-post (NOT enabled)

If you ever have a large 18Birdies-only backlog, here's the recon so it can be
built behind a **dry-run you approve** (no blind writes):

**Auth:** paste a GHIN `Bearer` JWT (same one used above; `scp:"user"`, ~12h life).
Posting your *own* scores needs no elevated scope. Never store the password.

**Flow per round (after dedupe):**
1. `GET /courses/search.json?name={course}&country=USA&include_tee_sets=true` → `course_id`
2. `GET /courses/{course_id}/tee_set_ratings.json?gender=M&number_of_holes=18` →
   `tee_set_id`, course rating, slope (match by your tee)
3. `GET /golfers/{ghin}/scores.json` → **dedupe** (skip rounds Arccos already posted)
4. `POST /scores/hbh.json` (hole-by-hole — let GHIN do the WHS adjustment), body with
   `golfer_id, course_id, tee_set_id, played_at, score_type (H/A), number_of_holes,
   hole-by-hole scores`. Keep `allow_duplicates=false`.
   - Fallback: `POST /scores/adjusted.json` with a pre-adjusted gross.
5. Undo a mistake: `DELETE /scores/{id}.json`; fix: `PATCH /scores/{id}/update.json`.

**Hard gates any auto-poster must have:** dedupe against existing GHIN scores,
correct tee/rating/slope (verified, not guessed), `score_type` never `T` unless a
real tournament, and a printed **dry-run the user confirms** before any POST.

Base URL: `https://api2.ghin.com/api/v1`. Reference clients: `n8io/ghin` (npm),
official OpenAPI `GHIN/Admin/1.0` on SwaggerHub.
