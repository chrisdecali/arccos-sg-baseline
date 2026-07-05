#!/usr/bin/env python3
"""
pull_ghin.py — pull your official USGA/GHIN handicap + full score history into
the repo, alongside the Arccos data.

READ-ONLY: never posts a score. Password lives in the OS keychain when available (plaintext file only with explicit consent). Auth is a short-lived
Bearer JWT (~12h) you paste from GHIN.com's DevTools — exactly like the Arccos flow.

AUTH — put in ~/.ghin_creds.json (chmod 600; gitignored):
    {"bearer_token": "eyJ...", "ghin_id": "1234567"}
  Get the token: log into GHIN.com -> DevTools -> Network -> click any request to
  api2.ghin.com -> Request Headers -> copy the "Authorization: Bearer ..." value.
  Your ghin_id is your GHIN number (also visible in those request URLs as
  /golfers/{ghin_id}/...). Env vars GHIN_BEARER / GHIN_ID also work.

Endpoints (host api2.ghin.com/api/v1, confirmed via public OpenAPI + open clients):
    GET /golfers/search.json?golfer_id={ghin}     profile + handicap index
    GET /golfers/{ghin}/scores.json               full score history
    GET /golfers/{ghin}/handicap_history.json     index revision history

Outputs (./arccos_out/, public-safe — no name/email/GHIN# in the committed files):
    ghin_scores.csv             one row per posted score (date, course, rating/slope,
                                adjusted gross, differential, type, used-in-calc)
    ghin_handicap_history.csv   index over time
    ghin_profile.json           redacted: index, low index, club, association, state

Usage:
    python3 pull_ghin.py --discover   # dump raw JSON structure (verify fields)
    python3 pull_ghin.py              # fetch + build the CSVs/JSON
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

GHIN_BASE = "https://api2.ghin.com/api/v1"
CREDS_PATH = os.path.expanduser("~/.ghin_creds.json")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("GOLF_STORE") or (
    os.path.join(HERE, "arccos_out") if os.path.isdir(os.path.join(HERE, "arccos_out")) else HERE)
CACHE = os.path.join(OUT_DIR, "_cache_ghin")     # gitignored (_cache*)
DELAY_S = 0.6
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148 Safari/537.36"


# ---------------------------------------------------------------------------
# Credentials (read-only; never printed)
# ---------------------------------------------------------------------------

AUTH_URL = f"{GHIN_BASE}/golfer_login.json"
FIREBASE_URL = "https://firebaseinstallations.googleapis.com/v1/projects/ghin-mobile-app/installations"
FIREBASE_KEY = "AIzaSyBxgTOAWxiud0HuaE5tN-5NTlzFnrtyz-I"
FIREBASE_APPID = "1:884417644529:web:47fb315bc6c70242f72650"


def _post(url: str, body: dict, headers: Optional[dict] = None) -> Optional[Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        time.sleep(DELAY_S)
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  POST {url.rsplit('/',1)[-1]} -> HTTP {e.code}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  POST {url.rsplit('/',1)[-1]} -> {type(e).__name__}", file=sys.stderr)
    return None


def _firebase_preauth() -> str:
    """Pre-auth token GHIN's login wants. A literal placeholder usually passes; if
    not, mint a Firebase Installations token (what the GHIN web app does)."""
    import base64
    fid = base64.urlsafe_b64encode(os.urandom(17)).decode().rstrip("=")[:22]
    d = _post(FIREBASE_URL, {"appId": FIREBASE_APPID, "authVersion": "FIS_v2",
                             "sdkVersion": "w:0.5.7", "fid": fid},
              {"x-goog-api-key": FIREBASE_KEY})
    return ((d or {}).get("authToken") or {}).get("token") or "nonblank"


def ghin_login(email_or_ghin: str, password: str) -> tuple[Optional[str], Optional[str]]:
    """Email/GHIN + password -> (golfer_user_token ~12h, golfer_id). Tries a
    placeholder pre-auth token first, then a real Firebase token on failure."""
    for preauth in ("nonblank", None):
        tok = preauth if preauth else _firebase_preauth()
        d = _post(AUTH_URL, {"token": tok, "user": {
            "email_or_ghin": email_or_ghin, "password": password, "remember_me": True}})
        gu = (d or {}).get("golfer_user") if isinstance(d, dict) else None
        if gu and gu.get("golfer_user_token"):
            return gu["golfer_user_token"], str(gu.get("golfer_id") or "")
    return None, None


def load_creds() -> tuple[str, str]:
    """Return (jwt, ghin_id). Auto: email+password in creds/env -> fresh 12h JWT each
    run (re-login, no manual paste). Manual fallback: a pasted bearer_token."""
    token = os.environ.get("GHIN_BEARER")
    ghin = os.environ.get("GHIN_ID")
    email = os.environ.get("GHIN_EMAIL")
    password = os.environ.get("GHIN_PASSWORD")
    if os.path.exists(CREDS_PATH):
        try:
            with open(CREDS_PATH, encoding="utf-8") as f:
                c = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            sys.exit(f"Error: could not read {CREDS_PATH}: {e}")
        token = token or c.get("bearer_token") or c.get("token")
        ghin = ghin or c.get("ghin_id") or c.get("ghin") or c.get("golfer_id")
        email = email or c.get("email") or c.get("email_or_ghin") or c.get("username")
        password = password or c.get("password")
        if email and not password:
            try:
                import keyring  # optional; OS keychain
                password = keyring.get_password("golf-reports-ghin", email)
            except Exception:
                print("  note: OS keychain unavailable — set GHIN_PASSWORD env or re-run setup.py", file=sys.stderr)
                password = None
    if token:
        token = token.strip()
        for pre in ("Bearer:", "Bearer"):
            if token.startswith(pre):
                token = token[len(pre):].strip()
    # Prefer auto re-login (always fresh) when email+password are present.
    if email and password:
        fresh, gid = ghin_login(email, password)
        if fresh:
            token = fresh
            ghin = ghin or gid
        elif not token:
            sys.exit("Error: GHIN login failed and no fallback bearer_token. "
                     "Check email/password in " + CREDS_PATH)
    if not token or not ghin:
        sys.exit(f"Error: missing GHIN creds. Put EITHER email+password (auto) OR a "
                 f'bearer_token (manual) + ghin_id in {CREDS_PATH}.')
    return token, str(ghin)


# ---------------------------------------------------------------------------
# HTTP (read-only GET)
# ---------------------------------------------------------------------------

_RETRY_CODES = (500, 502, 503, 504)


def _with_retry(fn, attempts: int = 3, base_delay: float = 2.0):
    """Retry transient failures (5xx, timeouts, connection drops) with backoff.
    4xx and other HTTPErrors raise immediately. Duplicated in both pullers on
    purpose — no shared module, the ~ copies are symlinks (see plan Task 8)."""
    for i in range(attempts):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code not in _RETRY_CODES or i == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if i == attempts - 1:
                raise
        time.sleep(base_delay * (2 ** i))


def ghin_get(path: str, token: str, params: Optional[dict] = None, soft: bool = False) -> Any:
    q = dict(params or {})
    q.setdefault("source", "GHINcom")
    url = f"{GHIN_BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    def _go():
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    try:
        time.sleep(DELAY_S)
        return _with_retry(_go)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("Error: 401 Unauthorized — GHIN session rejected. "
                     "If you set up with email/password: re-run setup.py (password may have changed). "
                     "If you pasted a manual bearer_token: it expires ~12h — paste a fresh one.")
        if soft:
            print(f"  {path} -> HTTP {e.code}", file=sys.stderr)
            return None
        sys.exit(f"Error: HTTP {e.code} for {path}")
    except Exception as e:  # noqa: BLE001
        if soft:
            print(f"  {path} -> {type(e).__name__}", file=sys.stderr)
            return None
        sys.exit(f"Error: {type(e).__name__} for {path}: {e}")


def _save(name: str, obj: Any) -> None:
    os.makedirs(CACHE, exist_ok=True)
    target = os.path.join(CACHE, name)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, target)


def _keys(o: Any) -> Any:
    if isinstance(o, dict):
        return sorted(o.keys())
    if isinstance(o, list) and o:
        return f"list[{len(o)}] -> {_keys(o[0])}"
    return type(o).__name__


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(token: str, ghin: str) -> dict:
    prof = ghin_get("/golfers/search.json", token,
                    {"golfer_id": ghin, "status": "Active", "per_page": "10", "page": "1"}, soft=True)
    scores = ghin_get(f"/golfers/{ghin}/scores.json", token, soft=True)
    hist = ghin_get(f"/golfers/{ghin}/handicap_history.json", token,
                    {"rev_count": "0", "include_hidden": "false"}, soft=True)
    _save("profile.json", prof)
    _save("scores.json", scores)
    _save("handicap_history.json", hist)
    return {"profile": prof, "scores": scores, "history": hist}


# ---------------------------------------------------------------------------
# Build outputs (defensive .get — GHIN field names vary slightly by endpoint)
# ---------------------------------------------------------------------------

def _g(d: dict, *names, default=None):
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d.get(n)
    return default


SCORE_COLS = ["played_at", "course_name", "holes", "adjusted_gross_score", "course_rating",
              "slope_rating", "differential", "score_type", "status", "used", "exceptional",
              "posted_at", "score_id"]
HIST_COLS = ["date", "handicap_index", "low_hi", "is_low_hi"]
HOLE_SCORE_COLS = [
    "score_id", "played_at", "course_name", "hole_number", "par", "raw_score",
    "adjusted_gross_score", "stroke_allocation", "putts", "fairway_hit", "gir_flag",
    "drive_accuracy", "approach_shot_accuracy", "x_hole", "most_likely_score",
]


def build_hole_scores(scores: Any) -> list[dict]:
    """Extract per-hole detail rows from GHIN score buckets.

    Iterates recent_scores and revision_scores (deduped by score id) and emits
    one row per hole_detail entry. Many fields (putts, fairway_hit, gir_flag, etc.)
    are null for manually posted scores — that is expected; write null/empty.
    Returns rows sorted by played_at desc, then hole_number asc.
    """
    if not isinstance(scores, dict):
        return []
    rows: list[dict] = []
    seen: set = set()
    for k in ("recent_scores", "revision_scores", "9_hole_score", "scores", "results"):
        v = scores.get(k)
        if isinstance(v, dict):
            v = v.get("scores")
        if not isinstance(v, list):
            continue
        for s in v:
            if not isinstance(s, dict):
                continue
            sid = s.get("id")
            if sid is not None and sid in seen:
                continue
            if sid is not None:
                seen.add(sid)
            played_at = (_g(s, "played_at", "date_played", default="") or "")[:10]
            course_name = _g(s, "course_name", "course_display_value", default="")
            for hd in (s.get("hole_details") or []):
                if not isinstance(hd, dict):
                    continue
                rows.append({
                    "score_id": sid,
                    "played_at": played_at,
                    "course_name": course_name,
                    "hole_number": _g(hd, "hole_number", "hole"),
                    "par": _g(hd, "par"),
                    "raw_score": _g(hd, "raw_score", "gross_score", "score"),
                    "adjusted_gross_score": _g(hd, "adjusted_gross_score", "adjusted_score"),
                    "stroke_allocation": _g(hd, "stroke_allocation", "handicap_stroke"),
                    "putts": _g(hd, "putts", "number_of_putts"),
                    "fairway_hit": _g(hd, "fairway_hit"),
                    "gir_flag": _g(hd, "gir_flag", "green_in_regulation"),
                    "drive_accuracy": _g(hd, "drive_accuracy"),
                    "approach_shot_accuracy": _g(hd, "approach_shot_accuracy"),
                    "x_hole": _g(hd, "x_hole"),
                    "most_likely_score": _g(hd, "most_likely_score"),
                })
    rows.sort(key=lambda r: (
        r.get("played_at") or "", r.get("hole_number") or 0
    ), reverse=False)
    # Re-sort: newest first by played_at, then hole ascending within a round.
    rows.sort(key=lambda r: r.get("played_at") or "", reverse=True)
    return rows


def _scores_list(scores: Any) -> list[dict]:
    """GHIN returns scores in buckets (recent_scores, revision_scores, 9_hole_score),
    not a flat list. Merge them, dedupe by id, exclude deleted_scores."""
    if isinstance(scores, list):
        return scores
    if not isinstance(scores, dict):
        return []
    out, seen = [], set()
    for k in ("recent_scores", "revision_scores", "9_hole_score", "scores", "results"):
        v = scores.get(k)
        if isinstance(v, dict):       # GHIN wraps each bucket: {'scores': [...]}
            v = v.get("scores")
        if not isinstance(v, list):
            continue
        for s in v:
            sid = s.get("id") if isinstance(s, dict) else None
            if sid is not None and sid in seen:
                continue
            if sid is not None:
                seen.add(sid)
            out.append(s)
    return out


def build_scores(scores: Any) -> list[dict]:
    rows = []
    for s in _scores_list(scores):
        rows.append({
            "played_at": (_g(s, "played_at", "date_played", default="") or "")[:10],
            "course_name": _g(s, "course_name", "course_display_value", default=""),
            "holes": _g(s, "number_of_holes", "holes"),
            "adjusted_gross_score": _g(s, "adjusted_gross_score", "score"),
            "course_rating": _g(s, "course_rating"),
            "slope_rating": _g(s, "slope_rating"),
            "differential": _g(s, "differential"),
            "score_type": _g(s, "score_type_display_full", "score_type"),
            "status": _g(s, "status"),
            "used": 1 if _g(s, "used") in (True, "true", 1, "1") else (0 if _g(s, "used") is not None else ""),
            "exceptional": 1 if _g(s, "exceptional") in (True, "true", 1) else 0,
            "posted_at": (_g(s, "posted_at", default="") or "")[:10],
            "score_id": _g(s, "id", "score_id"),
        })
    rows.sort(key=lambda r: r.get("played_at") or "", reverse=True)
    return rows


def build_history(hist: Any) -> list[dict]:
    revs = hist
    if isinstance(hist, dict):
        revs = _g(hist, "handicap_revisions", "revisions", "handicap_history", default=[])
    rows = []
    for r in (revs or []):
        rows.append({
            "date": (_g(r, "revision_date", "date", "rev_date", default="") or "")[:10],
            "handicap_index": _g(r, "display", "handicap_index", "value"),
            "low_hi": _g(r, "low_hi", "low_handicap_index", "low_hi_display"),
            "is_low_hi": 1 if _g(r, "is_low_hi") in (True, "true", 1) else 0,
        })
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return rows


def build_profile(prof: Any) -> dict:
    g = prof
    if isinstance(prof, dict):
        gl = _g(prof, "golfers", "golfer", default=None)
        if isinstance(gl, list) and gl:
            g = gl[0]
        elif isinstance(gl, dict):
            g = gl
    g = g or {}
    idx = _g(g, "hi_display", "handicap_index", "hi_value")
    if idx in (999, 999.0, "999", "999.0"):
        idx = "NH"  # GHIN sentinel: not enough scores for an index yet
    # PUBLIC-SAFE: keep performance fields; drop name / email / GHIN number.
    return {
        "handicap_index": idx,
        "low_hi": _g(g, "low_hi", "low_hi_display"),
        "low_hi_date": _g(g, "low_hi_date"),
        "rev_date": _g(g, "rev_date"),
        "soft_cap": _g(g, "soft_cap"),
        "hard_cap": _g(g, "hard_cap"),
        "gender": _g(g, "gender"),
        "status": _g(g, "status"),
        "club_name": _g(g, "club_name"),
        "association_name": _g(g, "association_name"),
        "state": _g(g, "state"),
        "country": _g(g, "country"),
        "_note": "Official USGA/GHIN. Redacted: name, email, GHIN number removed. "
                 "Index is the WHS Handicap Index (NOT Arccos' proprietary scale).",
    }


def write_csv(path: str, cols: list[str], rows: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    os.replace(tmp, path)


def build(data: dict) -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    scores = build_scores(data.get("scores"))
    hist = build_history(data.get("history"))
    prof = build_profile(data.get("profile"))
    write_csv(os.path.join(OUT_DIR, "ghin_scores.csv"), SCORE_COLS, scores)
    write_csv(os.path.join(OUT_DIR, "ghin_handicap_history.csv"), HIST_COLS, hist)
    # ghin_hole_scores.csv — per-hole detail rows (non-GPS, always published).
    # Many stat fields (putts, gir_flag, etc.) are null for manually-posted scores;
    # that is normal GHIN behaviour — the row is written with empty values regardless.
    hole_scores = build_hole_scores(data.get("scores"))
    write_csv(os.path.join(OUT_DIR, "ghin_hole_scores.csv"), HOLE_SCORE_COLS, hole_scores)
    profile_path = os.path.join(OUT_DIR, "ghin_profile.json")
    tmp = profile_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prof, f, indent=2)
    os.replace(tmp, profile_path)
    return {"scores": len(scores), "revisions": len(hist), "index": prof.get("handicap_index"),
            "hole_scores": len(hole_scores)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def discover(token: str, ghin: str) -> None:
    data = fetch(token, ghin)
    print(f"profile keys: {_keys(data['profile'])}")
    print(f"scores keys:  {_keys(data['scores'])}")
    sl = _scores_list(data["scores"])
    if sl:
        print(f"score[0] keys: {_keys(sl[0])}")
        print(json.dumps(sl[0], indent=2)[:1200])
    print(f"history keys: {_keys(data['history'])}")
    print(f"raw saved -> {CACHE}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--discover", action="store_true", help="Dump raw JSON structure, no parsing.")
    # Optional path overrides (defaults unchanged when flags absent). --golfer-id fetches
    # a different golfer under the same session (creds file supplies the auth token only).
    p.add_argument("--creds", type=str, default=None,
                   help="Path to GHIN creds JSON (default: ~/.ghin_creds.json).")
    p.add_argument("--out", type=str, default=None,
                   help="Output directory (default: ./arccos_out, or this script's dir).")
    p.add_argument("--golfer-id", type=str, default=None, dest="golfer_id",
                   help="Override the GHIN golfer id to fetch (default: creds file's ghin_id).")
    args = p.parse_args()

    global CREDS_PATH, OUT_DIR, CACHE
    if args.creds:
        CREDS_PATH = os.path.expanduser(args.creds)
    if args.out:
        OUT_DIR = os.path.abspath(args.out)
        CACHE = os.path.join(OUT_DIR, "_cache_ghin")
    token, ghin = load_creds()
    if args.golfer_id:
        ghin = str(args.golfer_id)
    if args.discover:
        discover(token, ghin)
        return
    counts = build(fetch(token, ghin))
    print(f"BUILT: ghin_scores={counts['scores']}, handicap_revisions={counts['revisions']}, "
          f"index={counts['index']}")
    print(f"Outputs -> {OUT_DIR}")


if __name__ == "__main__":
    main()
