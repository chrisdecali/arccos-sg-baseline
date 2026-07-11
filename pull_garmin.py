#!/usr/bin/env python3
"""
pull_garmin.py - Garmin Golf scorecard puller using garth.

Dependency:
    python -m pip install garth

Auth/session handling and Garmin Golf endpoint paths are aligned with the
captured python-garminconnect/garth sources:
  - /gcs-golfcommunity/api/v2/scorecard/summary
  - /gcs-golfcommunity/api/v2/scorecard/detail
  - /gcs-golfcommunity/api/v2/shot/scorecard/{id}/hole

Worker contract:
    python pull_garmin.py --creds <creds.json> --out <out_dir> --token-out <token_file>

creds.json is {"kind":"password"|"token","secret":"..."}.
For kind=password, secret is a JSON string {"email":"...","password":"..."}.
The script logs in, writes garth.client.dumps() to --token-out, then pulls data.
For kind=token, secret is the garth session string and is loaded with
garth.client.loads().

Outputs:
  garmin_rounds.csv  Garmin-native round summary rows.
  garmin_holes.csv   Garmin-native per-hole score rows.
  garmin_shots.csv   Garmin-native per-shot rows.

TODO(canonical mapping):
  Map these Garmin-native CSVs onto canonical rounds_summary.csv/shots.csv in
  the next importer pass. Garmin does not provide native strokes-gained.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import garth
    from garth.exc import GarthException, GarthHTTPError
except ImportError:  # handled in main so --help still works
    garth = None
    GarthException = Exception  # type: ignore[assignment]
    GarthHTTPError = Exception  # type: ignore[assignment]


ENDPOINTS = {
    "scorecard_summary": "/gcs-golfcommunity/api/v2/scorecard/summary",
    "scorecard_detail": "/gcs-golfcommunity/api/v2/scorecard/detail",
    "hole_shots": "/gcs-golfcommunity/api/v2/shot/scorecard/{round_id}/hole",
}

ROUND_COLS = [
    "round_id", "date", "formattedStartTime", "course", "holes", "score",
    "playerHandicap", "putts", "pars", "birdies", "bogeys", "eagles",
    "double_bogeys", "penalties", "pulled_at",
]

HOLE_COLS = [
    "round_id", "hole", "par", "strokes", "putts", "penalties",
    "fairwayShotOutcome",
]

SHOT_COLS = [
    "round_id", "hole", "shotOrder", "clubId", "start_lie", "end_lie",
    "distance_yds", "shotType", "start_lat", "start_lng", "end_lat",
    "end_lng", "shot_id", "scorecardId",
]

YD_PER_M = 1.0936132983


def die_auth(message: str = "unauthorized") -> None:
    print(f"unauthorized: {message}", file=sys.stderr)
    sys.exit(1)


def die_rate(message: str = "too many requests") -> None:
    print(f"429: {message}", file=sys.stderr)
    sys.exit(1)


def _http_status(exc: BaseException) -> Optional[int]:
    response = getattr(getattr(exc, "error", None), "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _handle_garth_error(exc: BaseException, context: str, soft: bool = False) -> Any:
    code = _http_status(exc)
    text = str(exc).lower()
    if (code in (401, 403) or "unauthorized" in text
            or (context == "Garmin login"
                and any(s in text for s in ("sso error", "invalid", "incorrect", "forbidden")))):
        die_auth(f"{context} failed")
    if code == 429 or "too many requests" in text:
        die_rate(f"{context} rate limited")
    if soft:
        print(f"Warning: {context} failed ({type(exc).__name__})", file=sys.stderr)
        return None
    print(f"Error: {context} failed ({type(exc).__name__})", file=sys.stderr)
    sys.exit(1)


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
        value = _g(d, field)
        n = _num(value)
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


def authenticate(kind: str, secret: str, token_out: str) -> Optional[str]:
    if garth is None:
        print("Error: missing dependency garth. Install with: python -m pip install garth", file=sys.stderr)
        sys.exit(1)
    try:
        garth.configure(timeout=30, telemetry_enabled=False)
    except Exception:
        pass
    if kind == "password":
        email, password = parse_password_secret(secret)
        try:
            result = garth.login(email, password, return_on_mfa=True)
        except Exception as exc:  # noqa: BLE001
            _handle_garth_error(exc, "Garmin login")
        mfa_marker = str(result).lower()
        if (callable(result)
                or (isinstance(result, (tuple, list)) and any("mfa" in str(x).lower() for x in result))
                or "mfa" in mfa_marker):
            die_auth("Garmin MFA required; noninteractive worker login cannot complete MFA")
        try:
            token = garth.client.dumps()
        except Exception as exc:  # noqa: BLE001
            print(f"Error: could not serialize Garmin session ({type(exc).__name__})", file=sys.stderr)
            sys.exit(1)
        write_token(token_out, token)
        return None
    try:
        garth.client.loads(secret.strip())
    except Exception as exc:  # noqa: BLE001
        die_auth(f"Garmin token resume failed ({type(exc).__name__})")
    return secret.strip()


def garmin_get(path: str, params: Optional[dict[str, Any]] = None, soft: bool = False) -> Any:
    try:
        return garth.connectapi(path, params=params or {})
    except Exception as exc:  # noqa: BLE001
        return _handle_garth_error(exc, path, soft=soft)


def _summary_rows(obj: Any) -> list[dict]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []
    for key in ("scorecardSummaries", "scorecards", "rounds", "items", "results"):
        rows = obj.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def _summary_round_id(summary: dict) -> Any:
    return _g(summary, "id", "scorecardId", "scorecardID", "roundId", "roundID")


def _detail_items(detail: Any) -> list[dict]:
    if isinstance(detail, list):
        return [x for x in detail if isinstance(x, dict)]
    if not isinstance(detail, dict):
        return []
    details = detail.get("details")
    if isinstance(details, list):
        return [x for x in details if isinstance(x, dict)]
    return [detail]


def _scorecard_detail(detail: Any) -> dict:
    for item in _detail_items(detail):
        scorecard_details = item.get("scorecardDetails")
        if isinstance(scorecard_details, list):
            for scorecard_detail in scorecard_details:
                if isinstance(scorecard_detail, dict):
                    return scorecard_detail
        if isinstance(item.get("scorecard"), dict):
            return item
    return {}


def _scorecard(detail: Any) -> dict:
    scorecard_detail = _scorecard_detail(detail)
    if isinstance(scorecard_detail.get("scorecard"), dict):
        return scorecard_detail["scorecard"]
    if not isinstance(detail, dict):
        return {}
    if isinstance(detail.get("scorecard"), dict):
        return detail["scorecard"]
    return {}


def _stats_round(detail: Any) -> dict:
    scorecard_detail = _scorecard_detail(detail)
    stats = scorecard_detail.get("scorecardStats")
    if isinstance(stats, dict) and isinstance(stats.get("round"), dict):
        return stats["round"]
    if isinstance(stats, dict):
        return stats
    for item in _detail_items(detail):
        stats = item.get("scorecardStats")
        if isinstance(stats, dict) and isinstance(stats.get("round"), dict):
            return stats["round"]
    return {}


def _course_snapshot(detail: Any) -> dict:
    scorecard_detail = _scorecard_detail(detail)
    snapshots = scorecard_detail.get("courseSnapshots")
    if isinstance(snapshots, list):
        for snapshot in snapshots:
            if isinstance(snapshot, dict):
                return snapshot
    for item in _detail_items(detail):
        snapshots = item.get("courseSnapshots")
        if isinstance(snapshots, list):
            for snapshot in snapshots:
                if isinstance(snapshot, dict):
                    return snapshot
    return {}


def _holes(card: dict) -> list[dict]:
    holes = _g(card, "holes", "holeScores", "scorecardHoles")
    if isinstance(holes, list):
        return [h for h in holes if isinstance(h, dict)]
    return []


def _hole_number(hole: dict) -> Optional[int]:
    return _int(_g(hole, "number", "holeNumber", "hole", "holeNum"))


def _hole_pars(snapshot: dict) -> list[Any]:
    pars = snapshot.get("holePars") if isinstance(snapshot, dict) else None
    return pars if isinstance(pars, list) else []


def _par_for_hole(hole_number: Optional[int], hole: dict, snapshot: dict) -> Any:
    direct = _g(hole, "par")
    if direct is not None:
        return direct
    if hole_number is None:
        return None
    for idx, item in enumerate(_hole_pars(snapshot), start=1):
        if isinstance(item, dict):
            number = _int(_g(item, "number", "holeNumber", "hole", "holeNum", default=idx))
            if number == hole_number:
                return _g(item, "par", "value")
        elif idx == hole_number:
            return item
    return None


def _sum_holes(holes: list[dict], *fields: str) -> Optional[int]:
    vals = []
    for h in holes:
        v = _int(_g(h, *fields))
        if v is not None:
            vals.append(v)
    return sum(vals) if vals else None


def _count_truthy(holes: list[dict], *fields: str) -> tuple[Optional[int], Optional[int]]:
    vals = [_truthy(_g(h, *fields)) for h in holes]
    vals = [v for v in vals if v is not None]
    return (sum(vals), len(vals)) if vals else (None, None)


def round_row(summary: dict, detail: Any, pulled_at: str) -> dict:
    card = _scorecard(detail)
    stats = _stats_round(detail)
    snapshot = _course_snapshot(detail)
    holes = _holes(card)
    rid = _g(card, "id", default=_summary_round_id(summary))
    score = _g(card, "score", default=_sum_holes(holes, "strokes", "score"))
    putts = _g(stats, "putts", "totalPutts", default=_sum_holes(holes, "putts"))
    penalties = _g(card, "penalties", "penaltyStrokes", default=_sum_holes(holes, "penalties", "penaltyStrokes"))
    return {
        "round_id": rid,
        "date": _date(_g(card, "startTime", default=_g(summary, "startTime", "date"))),
        "formattedStartTime": _g(card, "formattedStartTime"),
        "course": _g(snapshot, "name", default=_g(card, "courseName", "course", default=_g(summary, "courseName", "course", default=""))),
        "holes": _g(card, "holesCompleted", default=(len(holes) or None)),
        "score": score,
        "playerHandicap": _g(card, "playerHandicap"),
        "putts": putts,
        "pars": _g(stats, "pars", "par", "parCount"),
        "birdies": _g(stats, "birdies", "birdie", "birdieCount"),
        "bogeys": _g(stats, "bogeys", "bogey", "bogeyCount"),
        "eagles": _g(stats, "eagles", "eagle", "eagleCount"),
        "double_bogeys": _g(stats, "doubleBogeys", "double_bogeys", "doubleBogeyCount"),
        "penalties": penalties,
        "pulled_at": pulled_at,
    }


def hole_rows(round_id: Any, detail: Any) -> list[dict]:
    card = _scorecard(detail)
    snapshot = _course_snapshot(detail)
    rows = []
    for hole in _holes(card):
        number = _hole_number(hole)
        rows.append({
            "round_id": round_id,
            "hole": number,
            "par": _par_for_hole(number, hole, snapshot),
            "strokes": _g(hole, "strokes", "score"),
            "putts": _g(hole, "putts"),
            "penalties": _g(hole, "penalties", "penaltyStrokes"),
            "fairwayShotOutcome": _g(hole, "fairwayShotOutcome"),
        })
    return rows


def _meter_to_yd(value: Any) -> Optional[float]:
    n = _num(value)
    return round(n * YD_PER_M, 1) if n is not None else None


def _loc(obj: Any) -> dict:
    return obj if isinstance(obj, dict) else {}


def _shot_items(payload: Any) -> list[tuple[Optional[int], dict]]:
    groups: list[Any]
    if isinstance(payload, dict):
        groups = payload.get("shotDetails") or payload.get("holeShots") or payload.get("holes") or []
    elif isinstance(payload, list):
        groups = payload
    else:
        groups = []
    out: list[tuple[Optional[int], dict]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        hole = _int(_g(group, "holeNumber", "hole", "holeNum"))
        shots = _g(group, "shots", "strokes", "shotData")
        if isinstance(shots, list):
            out.extend((hole, s) for s in shots if isinstance(s, dict))
        elif any(k in group for k in ("id", "scorecardId", "clubId", "shotOrder", "meters")):
            out.append((hole, group))
    return out


def shot_rows(round_id: Any, payload: Any) -> list[dict]:
    rows = []
    for hole, shot in _shot_items(payload):
        start = _loc(_g(shot, "startLoc", "startLocation"))
        end = _loc(_g(shot, "endLoc", "endLocation"))
        rows.append({
            "round_id": round_id,
            "hole": hole,
            "shotOrder": _g(shot, "shotOrder"),
            "clubId": _g(shot, "clubId"),
            "start_lie": _g(start, "lie"),
            "end_lie": _g(end, "lie"),
            "distance_yds": _meter_to_yd(_g(shot, "meters")),
            "shotType": _g(shot, "shotType"),
            "start_lat": _g(start, "lat"),
            "start_lng": _g(start, "lon", "lng"),
            "end_lat": _g(end, "lat"),
            "end_lng": _g(end, "lon", "lng"),
            "shot_id": _g(shot, "id"),
            "scorecardId": _g(shot, "scorecardId"),
        })
    return rows


def fetch_data(n: int, after: Optional[str]) -> tuple[list[dict], list[dict], list[dict]]:
    pulled_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rounds: list[dict] = []
    holes_out: list[dict] = []
    shots: list[dict] = []
    start = 0
    page_size = min(max(n, 50), 100)
    while len(rounds) < n:
        summary = garmin_get(
            ENDPOINTS["scorecard_summary"],
            params={"per-page": str(page_size), "start": str(start)},
        )
        summaries = _summary_rows(summary)
        if not summaries:
            break
        for s in summaries:
            if len(rounds) >= n:
                break
            rid = _summary_round_id(s)
            if not rid:
                continue
            sdate = _date(_g(s, "startTime", "startedDate", "date"))
            if after and sdate and sdate < after:
                continue
            detail = garmin_get(
                ENDPOINTS["scorecard_detail"],
                params={"scorecard-ids": str(rid), "include-longest-shot-distance": "true"},
            )
            rrow = round_row(s, detail, pulled_at)
            rid = rrow.get("round_id") or rid
            rounds.append(rrow)
            hrows = hole_rows(rid, detail)
            holes_out.extend(hrows)
            hole_nums = [r["hole"] for r in hrows if r.get("hole") is not None]
            if not hole_nums and _int(rrow.get("holes")):
                hole_nums = list(range(1, int(rrow["holes"]) + 1))
            if not hole_nums:
                continue
            payload = garmin_get(
                ENDPOINTS["hole_shots"].format(round_id=rid),
                params={"hole-numbers": ",".join(str(h) for h in hole_nums)},
                soft=True,
            )
            if payload is not None:
                shots.extend(shot_rows(rid, payload))
        if len(summaries) < page_size:
            break
        start += page_size
    rounds.sort(key=lambda r: r.get("date") or "", reverse=True)
    holes_out.sort(key=lambda r: (str(r.get("round_id") or ""), int(r.get("hole") or 0)))
    shots.sort(key=lambda r: (str(r.get("round_id") or ""), int(r.get("hole") or 0),
                              int(r.get("shotOrder") or 0)))
    return rounds, holes_out, shots


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
    p.add_argument("--out", required=True, help="Output directory for Garmin CSVs.")
    p.add_argument("--token-out", required=True, help="Where to write minted/refreshed garth session token.")
    p.add_argument("--n", type=int, default=50, help="Max recent rounds to pull (default 50).")
    p.add_argument("--after", type=str, default=None, help="Only rounds on/after YYYY-MM-DD.")
    args = p.parse_args()

    kind, secret = read_creds(args.creds)
    original_token = authenticate(kind, secret, args.token_out)
    rounds, holes, shots = fetch_data(args.n, args.after)
    if kind == "token" and original_token:
        try:
            refreshed = garth.client.dumps()
        except Exception:
            refreshed = original_token
        if refreshed and refreshed != original_token:
            write_token(args.token_out, refreshed)
    os.makedirs(args.out, exist_ok=True)
    write_csv(os.path.join(args.out, "garmin_rounds.csv"), ROUND_COLS, rounds)
    write_csv(os.path.join(args.out, "garmin_holes.csv"), HOLE_COLS, holes)
    write_csv(os.path.join(args.out, "garmin_shots.csv"), SHOT_COLS, shots)
    print(f"BUILT: garmin_rounds={len(rounds)}, garmin_holes={len(holes)}, garmin_shots={len(shots)}")
    print(f"Outputs -> {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
