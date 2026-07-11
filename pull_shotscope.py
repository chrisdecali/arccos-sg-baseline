#!/usr/bin/env python3
"""
pull_shotscope.py - Shot Scope dashboard API puller.

Shot Scope has no public API. This stdlib-only script uses the dashboard API
surface confirmed in the captured dashboard bundles.

Worker contract:
    python pull_shotscope.py --creds <creds.json> --out <out_dir> --token-out <token_file>

creds.json is {"kind":"password"|"token","secret":"..."}.
For kind=password, secret is a JSON string {"email":"...","password":"..."}.
The script logs in, writes an expiry-aware access_token cache to --token-out,
then pulls. For kind=token, secret may be either a plain access_token or that
cache JSON.

Outputs:
  shotscope_rounds.csv  Shot Scope round summary rows.
  shotscope_holes.csv   Per-hole score rows.
  shotscope_shots.csv   Per-shot rows, including native strokes-gained.

TODO(canonical mapping):
  Map these Shot Scope-native CSVs onto canonical rounds_summary.csv/shots.csv
  in the next importer pass.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from typing import Any, Optional


BASE_URL = "https://dashboard.shotscope.com"
AUTH_PATH = "/Token"
AUTH_EMAIL_FIELD = "username"
AUTH_PASSWORD_FIELD = "password"
ROUNDS_PATH = "/api/rounds/slim?modifiedSinceDate="
CLUBS_PATH = "/api/v2/clubs/users"
ROUND_LIST_FIELDS = ("rounds", "playedRounds", "results", "items", "data")
ROUND_ID_FIELDS = ("roundID", "roundId", "id")
TOKEN_AUTH_SCHEME = "Bearer"
UA = "Mozilla/5.0 (X11; Linux x86_64) ShotScopePuller/0.1"


ROUND_COLS = [
    "round_id", "date", "course", "score", "score_to_par", "handicap",
]

HOLE_COLS = [
    "round_id", "holeNumber", "par", "strokeIndex", "score", "putts",
    "penalties", "sandSave",
]

SHOT_COLS = [
    "round_id", "holeNumber", "shotNumber", "clubName", "lie",
    "distanceToPin", "strokesGained", "start_lat", "start_lng", "end_lat",
    "end_lng",
]

YD_PER_M = 1.0936132983


COOKIE_JAR = CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIE_JAR))


def die_auth(message: str = "unauthorized") -> None:
    print(f"unauthorized: {message}", file=sys.stderr)
    sys.exit(1)


def die_rate(message: str = "too many requests") -> None:
    print(f"429: {message}", file=sys.stderr)
    sys.exit(1)


def _url(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return BASE_URL.rstrip("/") + "/" + path.lstrip("/")


def _g(d: Any, *names: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    for name in names:
        if name in d and d[name] is not None:
            return d[name]
    return default


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    if isinstance(value, dict):
        return _num(_g(value, "value", "amount", "distance", "meters", "yards"))
    return None


def _int(value: Any) -> Optional[int]:
    n = _num(value)
    return int(n) if n is not None else None


def _date(value: Any) -> str:
    if not value:
        return ""
    s = str(value)
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        return s[:10]
    return s


def _truthy(value: Any) -> Optional[int]:
    if value is None:
        return None
    if value in (True, 1, "1", "true", "True", "T", "YES", "yes", "Y"):
        return 1
    if value in (False, 0, "0", "false", "False", "F", "NO", "no", "N"):
        return 0
    return None


def _pct(num: Any, den: Any) -> Optional[float]:
    n = _num(num)
    d = _num(den)
    if n is None or d in (None, 0):
        return None
    return round(100.0 * n / d, 1)


def _yd(d: Any, *fields: str) -> Optional[float]:
    for field in fields:
        n = _num(_g(d, field))
        if n is None:
            continue
        low = field.lower()
        if "meter" in low or low.endswith("_m") or low.endswith("metres"):
            return round(n * YD_PER_M, 1)
        return round(n, 1)
    return None


def _club_category(club: Any) -> str:
    name = str(club or "")
    if "Driver" in name:
        return "Driver"
    if "Wood" in name:
        return "Wood"
    if "Hybrid" in name:
        return "Hybrid"
    if "Iron" in name:
        return "Iron"
    if "Wedge" in name:
        return "Wedge"
    if "Putter" in name or name.lower() == "putter":
        return "Putter"
    return ""


def read_creds(path: str) -> tuple[str, str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: could not read creds JSON: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
    kind = data.get("kind")
    secret = data.get("secret")
    if kind not in ("password", "token") or not isinstance(secret, str):
        print("Error: creds must contain kind=password|token and string secret", file=sys.stderr)
        sys.exit(1)
    return kind, secret


def parse_password_secret(secret: str) -> tuple[str, str]:
    try:
        data = json.loads(secret)
    except json.JSONDecodeError:
        die_auth("password secret is not valid JSON")
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        die_auth("password secret missing email/password")
    return str(email), str(password)


def write_token(path: str, token: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
        f.write("\n")
    os.replace(tmp, path)


def _utc_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _token_from_secret(secret: str) -> str:
    raw = secret.strip()
    if not raw:
        die_auth("empty Shot Scope token")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(data, dict):
        return raw
    token = _g(data, "access_token", "token")
    if not token:
        die_auth("Shot Scope token cache missing access_token")
    expires_at = _num(_g(data, "expires_at"))
    if expires_at is None and _num(_g(data, "expires_in")) is not None and _num(_g(data, "obtained_at")) is not None:
        expires_at = float(_g(data, "obtained_at")) + float(_g(data, "expires_in"))
    if expires_at is not None and _utc_ts() >= int(expires_at):
        die_auth("Shot Scope token expired; refresh with kind=password")
    return str(token)


def _auth_header(token: str) -> str:
    token = token.strip()
    if token.lower().startswith(("bearer ", "token ")):
        return token
    return f"{TOKEN_AUTH_SCHEME} {token}" if TOKEN_AUTH_SCHEME else token


def _request(method: str, path: str, token: Optional[str] = None,
             body: Optional[Any] = None, form: bool = False,
             soft: bool = False, auth_request: bool = False) -> Any:
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": UA,
        "ngrok-skip-browser-warning": "1",
    }
    if token:
        headers["Authorization"] = _auth_header(token)
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_url(path), data=data, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=30) as resp:
            raw = resp.read()
            if resp.status == 204 or not raw:
                return None
            text = raw.decode("utf-8")
            return json.loads(text)
    except urllib.error.HTTPError as exc:
        if auth_request and exc.code in (400, 401, 403):
            die_auth(f"HTTP {exc.code}")
        if exc.code in (401, 403):
            die_auth(f"HTTP {exc.code}")
        if exc.code == 429:
            die_rate("too many requests")
        if soft:
            print(f"Warning: {method} {path} -> HTTP {exc.code}", file=sys.stderr)
            return None
        print(f"Error: {method} {path} -> HTTP {exc.code}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        if soft:
            print(f"Warning: {method} {path} returned non-JSON", file=sys.stderr)
            return None
        print(f"Error: {method} {path} returned non-JSON", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if soft:
            print(f"Warning: {method} {path} -> {type(exc).__name__}", file=sys.stderr)
            return None
        print(f"Error: {method} {path} -> {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)


def login(email: str, password: str) -> tuple[str, str]:
    body = {
        "grant_type": "password",
        AUTH_EMAIL_FIELD: email,
        AUTH_PASSWORD_FIELD: password,
    }
    data = _request("POST", AUTH_PATH, body=body, form=True, auth_request=True)
    if not isinstance(data, dict):
        die_auth("login returned no token")
    token = data.get("access_token")
    if not token:
        die_auth("login returned no access_token")
    obtained_at = _utc_ts()
    expires_in = _int(data.get("expires_in"))
    cache = {
        "access_token": str(token),
        "token_type": str(data.get("token_type") or "bearer"),
        "expires_in": expires_in,
        "obtained_at": obtained_at,
    }
    if expires_in is not None:
        cache["expires_at"] = obtained_at + max(expires_in - 60, 0)
    return str(token), json.dumps(cache, separators=(",", ":"))


def authenticate(kind: str, secret: str, token_out: str) -> str:
    if kind == "token":
        return _token_from_secret(secret)
    email, password = parse_password_secret(secret)
    token, cache = login(email, password)
    write_token(token_out, cache)
    return token


def _rounds_list(obj: Any) -> list[dict]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []
    for key in ROUND_LIST_FIELDS:
        rows = obj.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def fetch_rounds(token: str, n: int) -> list[dict]:
    data = _request("GET", ROUNDS_PATH, token=token)
    return _rounds_list(data)[:n]


def fetch_clubs(token: str) -> dict[str, dict]:
    data = _request("GET", CLUBS_PATH, token=token, soft=True)
    clubs: list[Any]
    if isinstance(data, list):
        clubs = data
    elif isinstance(data, dict):
        clubs = data.get("clubs") or data.get("items") or data.get("data") or []
    else:
        clubs = []
    out: dict[str, dict] = {}
    for club in clubs:
        if not isinstance(club, dict):
            continue
        cid = _g(club, "clubID", "ClubID", "id")
        if cid is not None:
            out[str(cid)] = club
    return out


def _holes(round_obj: dict) -> list[dict]:
    holes = _g(round_obj, "holes", "roundHoles")
    if isinstance(holes, list):
        return [h for h in holes if isinstance(h, dict)]
    return []


def _hole_data(hole: dict) -> dict:
    nested = hole.get("hole")
    return nested if isinstance(nested, dict) else hole


def _hole_shots(hole: dict) -> list[dict]:
    shots = hole.get("shots")
    if isinstance(shots, list):
        return [s for s in shots if isinstance(s, dict)]
    nested = hole.get("hole")
    if isinstance(nested, dict) and isinstance(nested.get("shots"), list):
        return [s for s in nested["shots"] if isinstance(s, dict)]
    return []


def _sum_holes(holes: list[dict], *fields: str) -> Optional[int]:
    vals = []
    for h in holes:
        v = _int(_g(_hole_data(h), *fields))
        if v is not None:
            vals.append(v)
    return sum(vals) if vals else None


def _count_truthy(holes: list[dict], *fields: str) -> tuple[Optional[int], Optional[int]]:
    vals = [_truthy(_g(h, *fields)) for h in holes]
    vals = [v for v in vals if v is not None]
    return (sum(vals), len(vals)) if vals else (None, None)


def _hole_number(hole: dict) -> Any:
    h = _hole_data(hole)
    return _g(h, "holeNumber", "holeNum", "number", "hole")


def round_row(r: dict) -> dict:
    holes = _holes(r)
    score = _g(r, "score", "totalShots", default=_sum_holes(holes, "score", "strokes"))
    return {
        "round_id": _g(r, *ROUND_ID_FIELDS),
        "date": _date(_g(r, "datePlayed", "startedDate", "startDate", "date")),
        "course": _g(r, "courseName", default=""),
        "score": score,
        "score_to_par": _g(r, "scoreToPar", "avgScoreVsPar", "roundScore"),
        "handicap": _g(r, "handicap"),
    }


def _putt_count(hole: dict, clubs: dict[str, dict]) -> Optional[int]:
    direct = _int(_g(_hole_data(hole), "putts"))
    if direct is not None:
        return direct
    count = 0
    found = False
    for shot in _hole_shots(hole):
        club = _club_name(shot, clubs)
        if str(club).lower() == "putter" or _g(shot, "lie") == "green":
            count += 1
            found = True
    return count if found else None


def hole_rows(round_id: Any, round_obj: dict, clubs: dict[str, dict]) -> list[dict]:
    rows = []
    for hole in _holes(round_obj):
        h = _hole_data(hole)
        rows.append({
            "round_id": round_id,
            "holeNumber": _hole_number(hole),
            "par": _g(h, "par"),
            "strokeIndex": _g(h, "strokeIndex", "si"),
            "score": _g(h, "score", "strokes"),
            "putts": _putt_count(hole, clubs),
            "penalties": _g(h, "penalties", "penaltyStrokes"),
            "sandSave": _g(h, "sandSave"),
        })
    return rows


def _club_name(shot: dict, clubs: dict[str, dict]) -> Any:
    name = _g(shot, "clubName")
    if name:
        return name
    cid = _g(shot, "clubID", "clubId")
    club = clubs.get(str(cid)) if cid is not None else None
    if isinstance(club, dict):
        return _g(club, "clubName", "name", "Name", "shortName")
    return None


def _nested_coord(obj: dict, parent: str, *fields: str) -> Any:
    nested = obj.get(parent)
    if isinstance(nested, dict):
        return _g(nested, *fields)
    return None


def shot_rows(round_id: Any, round_obj: dict, clubs: dict[str, dict]) -> list[dict]:
    rows = []
    for hole in _holes(round_obj):
        hole_number = _hole_number(hole)
        for shot in _hole_shots(hole):
            rows.append({
                "round_id": round_id,
                "holeNumber": _g(shot, "holeNumber", "holeNum", default=hole_number),
                "shotNumber": _g(shot, "shotNumber"),
                "clubName": _club_name(shot, clubs),
                "lie": _g(shot, "lie"),
                "distanceToPin": _g(shot, "distanceToPin"),
                "strokesGained": _g(shot, "strokesGained"),
                "start_lat": _g(shot, "startLat", "lat", default=_nested_coord(shot, "start", "lat")),
                "start_lng": _g(shot, "startLng", "startLon", "lng", "lon", default=_nested_coord(shot, "start", "lng", "lon")),
                "end_lat": _g(shot, "endLat", default=_nested_coord(shot, "end", "lat")),
                "end_lng": _g(shot, "endLng", "endLon", default=_nested_coord(shot, "end", "lng", "lon")),
            })
    return rows


def build_rows(token: str, n: int) -> tuple[list[dict], list[dict], list[dict]]:
    clubs = fetch_clubs(token)
    round_rows: list[dict] = []
    hole_rows_out: list[dict] = []
    shot_rows_out: list[dict] = []
    for summary in fetch_rounds(token, n):
        rid = _g(summary, *ROUND_ID_FIELDS)
        if not rid:
            continue
        rr = round_row(summary)
        round_rows.append(rr)
        hole_rows_out.extend(hole_rows(rid, summary, clubs))
        shot_rows_out.extend(shot_rows(rid, summary, clubs))
    round_rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    hole_rows_out.sort(key=lambda r: (str(r.get("round_id") or ""), int(r.get("holeNumber") or 0)))
    shot_rows_out.sort(key=lambda r: (str(r.get("round_id") or ""), int(r.get("holeNumber") or 0),
                                      int(r.get("shotNumber") or 0)))
    return round_rows, hole_rows_out, shot_rows_out


def write_csv(path: str, cols: list[str], rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    os.replace(tmp, path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--creds", required=True, help="Worker creds JSON path.")
    p.add_argument("--out", required=True, help="Output directory for Shot Scope CSVs.")
    p.add_argument("--token-out", required=True, help="Where to write minted/refreshed Shot Scope token.")
    p.add_argument("--n", type=int, default=50, help="Max recent rounds to pull (default 50).")
    args = p.parse_args()

    kind, secret = read_creds(args.creds)
    token = authenticate(kind, secret, args.token_out)
    rounds, holes, shots = build_rows(token, args.n)
    os.makedirs(args.out, exist_ok=True)
    write_csv(os.path.join(args.out, "shotscope_rounds.csv"), ROUND_COLS, rounds)
    write_csv(os.path.join(args.out, "shotscope_holes.csv"), HOLE_COLS, holes)
    write_csv(os.path.join(args.out, "shotscope_shots.csv"), SHOT_COLS, shots)
    print(f"BUILT: shotscope_rounds={len(rounds)}, shotscope_holes={len(holes)}, shotscope_shots={len(shots)}")
    print(f"Outputs -> {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
