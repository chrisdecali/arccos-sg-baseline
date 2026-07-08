#!/usr/bin/env python3
"""import_launch_monitor.py — vendor-agnostic launch-monitor CSV importer.

    python import_launch_monitor.py <export.csv> <out_store_dir> [--date YYYY-MM-DD] [--append]

Tee Box (and any sim/range) hands out a CSV from whatever launch monitor they run —
Trackman, Foresight/GCQuad, Uneekor, FlightScope, Garmin R10, SkyTrak, Rapsodo, Bushnell.
Their column names + units all differ. This normalizes ANY of them into the
`launch_monitor.csv` schema gen_tracker already reads (measured carry override + swing
metrics), so a Tee Box session sharpens the dashboard the moment the file lands.

Strategy: fuzzy-match each export column to a canonical field by keyword (units/case/
punctuation stripped), auto-detect units (mph vs m/s vs kph, yards vs metres) from the
header text, convert to mph + yards, normalize club names. Unmatched columns are ignored.
Never fatal — writes what it can, warns on the rest.
"""
import csv
import io
import sys
from pathlib import Path

# canonical output field -> keyword groups that identify it in a header.
# Each group is (must-have-any, must-not-have). Matching is on a normalized header
# (lowercased, alnum+space only). First field whose group matches wins.
FIELDS = {
    "club":            ([["club name", "club type", "club"]], [["speed", "path", "face", "head"]]),
    "carry_yd":        ([["carry"]], [[]]),
    "total_yd":        ([["total distance", "total", "distance"]], [["carry", "to pin", "swing"]]),
    "club_mph":        ([["club speed", "clubhead speed", "chs", "club head speed"]], [[]]),
    "ball_mph":        ([["ball speed", "ball"]], [["spin", "flight"]]),
    "smash":           ([["smash", "efficiency"]], [[]]),
    "launch_deg":      ([["launch angle", "vert launch", "launch v", "launch"]], [["dir", "direction", "horizontal", "side"]]),
    "launch_dir_deg":  ([["launch direction", "launch dir", "horiz launch", "azimuth", "side angle"]], [[]]),
    "spin_rpm":        ([["spin rate", "back spin", "total spin", "spin"]], [["axis", "tilt"]]),
    "spin_axis_deg":   ([["spin axis", "axis tilt", "spin tilt"]], [[]]),
    "attack_deg":      ([["attack angle", "angle of attack", "aoa", "attack"]], [[]]),
    "club_path_deg":   ([["club path", "swing path", "path"]], [["face"]]),
    "face_angle_deg":  ([["face angle", "club face", "face to target"]], [["path", "to path"]]),
    "face_to_path_deg":([["face to path", "face path", "ftp", "face rel"]], [[]]),
}
OUT_COLS = ["date", "club"] + [k for k in FIELDS if k not in ("club",)] + ["notes"]

# club-name normalization → the canonical names gen_tracker/bag use.
def norm_club(raw) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    low = s.lower().replace(".", "").replace("-", " ")
    low = " ".join(low.split())
    aliases = {
        "driver": "Driver", "dr": "Driver", "d": "Driver", "1w": "Driver",
        "3 wood": "3 Wood", "3w": "3 Wood", "3 metal": "3 Wood",
        "5 wood": "5 Wood", "5w": "5 Wood", "7 wood": "7 Wood", "7w": "7 Wood",
        "2 hybrid": "2 Hybrid", "3 hybrid": "3 Hybrid", "4 hybrid": "4 Hybrid",
        "5 hybrid": "5 Hybrid", "hybrid": "Hybrid", "3h": "3 Hybrid", "4h": "4 Hybrid", "5h": "5 Hybrid",
        "pitching wedge": "Pitching Wedge", "pw": "Pitching Wedge", "p": "Pitching Wedge",
        "gap wedge": "Gap Wedge", "gw": "Gap Wedge", "approach wedge": "Gap Wedge", "aw": "Gap Wedge",
        "sand wedge": "Sand Wedge", "sw": "Sand Wedge", "lob wedge": "Lob Wedge", "lw": "Lob Wedge",
        "putter": "Putter",
    }
    if low in aliases:
        return aliases[low]
    # "N iron"/"Ni" and numbered wedges (e.g. "50", "54", "58" degree wedge)
    import re
    m = re.match(r"^(\d{1,2})\s*(i|iron)$", low)
    if m:
        return f"{int(m.group(1))} Iron"
    m = re.match(r"^(\d{2})\s*(w|wedge|deg|degree)?$", low)
    if m and 44 <= int(m.group(1)) <= 64:
        return f"{int(m.group(1))} Wedge"
    m = re.match(r"^(\d{1,2})\s*(w|wood|metal)$", low)
    if m:
        return f"{int(m.group(1))} Wood"
    return s  # leave unknown as-is (better than dropping)


def _norm_header(h: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in (h or "").lower()).split())


def _match_field(header: str) -> str | None:
    h = _norm_header(header)
    if not h:
        return None
    for field, (groups, nots) in FIELDS.items():
        for keys in groups:
            if any(k in h for k in keys) and not any(any(n in h for n in ng) for ng in nots if ng):
                return field
    return None


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "n/a", "na"):
        return None
    try:
        return float(s)
    except ValueError:
        # strip a trailing unit token e.g. "150 yds", "95 mph"
        tok = s.split()[0] if s.split() else s
        try:
            return float(tok)
        except ValueError:
            return None


def _unit_flags(headers):
    """Detect metric distance / speed from header text (Trackman can export m + m/s)."""
    blob = " ".join(_norm_header(h) for h in headers)
    dist_m = (" m" in f" {blob} " or "metre" in blob or "meter" in blob) and "yd" not in blob and "yard" not in blob
    speed_ms = "m s" in blob or "mps" in blob
    speed_kph = "kph" in blob or "km h" in blob
    return dist_m, speed_ms, speed_kph


def import_csv(src: Path, date: str) -> list[dict]:
    raw = src.read_bytes().decode("utf-8-sig", errors="replace")
    # sniff delimiter
    try:
        dialect = csv.Sniffer().sniff(raw[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(raw), dialect=dialect)
    headers = reader.fieldnames or []
    colmap = {h: _match_field(h) for h in headers}
    mapped_fields = {f for f in colmap.values() if f}
    if "club" not in mapped_fields or "carry_yd" not in mapped_fields:
        sys.stderr.write(f"warn: could not find club + carry columns. Headers: {headers}\n"
                         f"      matched: { {h:f for h,f in colmap.items() if f} }\n")
    dist_m, speed_ms, speed_kph = _unit_flags(headers)
    YD = 1.09361 if dist_m else 1.0
    MPH = 2.23694 if speed_ms else (0.621371 if speed_kph else 1.0)

    rows = []
    for r in reader:
        out: dict = {c: "" for c in OUT_COLS}
        out["date"] = date
        for h, field in colmap.items():
            if not field:
                continue
            val = r.get(h)
            if field == "club":
                out["club"] = norm_club(val)
                continue
            n = _num(val)
            if n is None:
                continue
            if field in ("carry_yd", "total_yd"):
                n *= YD
            elif field in ("club_mph", "ball_mph"):
                n *= MPH
            out[field] = round(n, 1)
        if out["club"] and out.get("carry_yd") not in ("", None):
            rows.append(out)
    return rows


def main():
    args = sys.argv[1:]
    append = "--append" in args
    date = None
    if "--date" in args:
        date = args[args.index("--date") + 1]
    pos = [a for a in args if not a.startswith("--") and a != date]
    if len(pos) < 2:
        sys.exit("usage: import_launch_monitor.py <export.csv> <out_store_dir> [--date YYYY-MM-DD] [--append]")
    src, store = Path(pos[0]), Path(pos[1])
    if not date:
        # fall back to today via the file mtime day (no network); caller can pass --date
        import datetime
        date = datetime.date.fromtimestamp(src.stat().st_mtime).isoformat()
    rows = import_csv(src, date)
    if not rows:
        sys.exit("no usable rows found — check the export has club + carry columns")
    store.mkdir(parents=True, exist_ok=True)
    out = store / "launch_monitor.csv"
    exists = out.exists()
    mode = "a" if (append and exists) else "w"
    with open(out, mode, newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        if mode == "w":
            w.writeheader()
        for row in rows:
            w.writerow(row)
    per_club = {}
    for r in rows:
        per_club.setdefault(r["club"], []).append(r["carry_yd"])
    print(f"imported {len(rows)} shots ({len(per_club)} clubs) -> {out} ({'appended' if mode=='a' else 'written'})")
    for c, cs in sorted(per_club.items()):
        print(f"  {c}: {len(cs)} shots, avg carry {sum(cs)/len(cs):.0f}y")


if __name__ == "__main__":
    main()
