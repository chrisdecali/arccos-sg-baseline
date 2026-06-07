#!/usr/bin/env python3
"""
arccos_shots_geo.py — build mappable shot files from the LOCAL Arccos cache.

Reads the cached raw rounds (arccos_out/_cache_raw/rounds/*.json — which DO contain
GPS) and emits, per round, into arccos_out/maps/ (GITIGNORED — coords stay off the
public repo):
    round_<id>.kml       open in Google Earth / Google My Maps -> satellite + shot
                         lines -> print/export to PDF to send to friends
    round_<id>.geojson   same, for any GeoJSON tool (geojson.io, QGIS, Leaflet)
    shots_geo.csv        flat table: every shot's start/end/pin lat-long

These files are NOT pushed (the public repo stays coord-free). Run after a pull.

Usage:
    python3 arccos_shots_geo.py
"""

from __future__ import annotations

import csv
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from pull_arccos import CLUBTYPE, OUT_DIR
except Exception:  # noqa: BLE001
    CLUBTYPE = {}
    OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arccos_out")


def haversine_yd(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * 6371000.0 * math.asin(math.sqrt(a)) * 1.0936132983, 1)

CACHE_ROUNDS = os.path.join(OUT_DIR, "_cache_raw", "rounds")
MAPS_DIR = os.path.join(OUT_DIR, "maps")

# Line colour per shot category (KML aabbggrr).
KML_COLOR = {"tee": "ff0000ff", "approach": "ff00a5ff", "short": "ff00ff00", "putt": "ffff0000"}


def _cat(idx, n, putts):
    if idx == 0:
        return "tee"
    if idx >= n - putts and putts > 0:
        return "putt"
    if idx == n - putts - 1:
        return "approach"
    return "short"


def round_features(detail: dict):
    """Yield (hole, shot_idx, club, dist, cat, start(lat,lng), end(lat,lng), pin(lat,lng))."""
    for h in (detail.get("holes") or []):
        if h.get("shouldIgnore") == "T":
            continue
        hid = h.get("holeId")
        pin = (h.get("pinLat"), h.get("pinLong"))
        shots = [s for s in (h.get("shots") or []) if s.get("shouldIgnore") != "T"]
        putts = h.get("putts") or 0
        n = len(shots)
        for i, s in enumerate(shots):
            sl, sg = s.get("startLat"), s.get("startLong")
            el, eg = s.get("endLat"), s.get("endLong")
            if sl is None or sg is None:
                continue
            club = CLUBTYPE.get(s.get("clubType")) or f"club{s.get('clubId')}"
            dist = haversine_yd(sl, sg, el, eg) if el is not None else None
            yield (hid, i + 1, club, dist, _cat(i, n, putts), (sl, sg),
                   (el, eg) if el is not None else None, pin)


def build_kml(detail: dict, rid, course, date) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
           f'<name>Arccos {course} {date} (round {rid})</name>']
    for cat, color in KML_COLOR.items():
        out.append(f'<Style id="{cat}"><LineStyle><color>{color}</color><width>3</width>'
                   f'</LineStyle><IconStyle><color>{color}</color></IconStyle></Style>')
    cur_hole = None
    feats = list(round_features(detail))
    for hid, sn, club, dist, cat, start, end, pin in feats:
        if hid != cur_hole:
            if cur_hole is not None:
                out.append('</Folder>')
            out.append(f'<Folder><name>Hole {hid}</name>')
            cur_hole = hid
            if pin and None not in pin:
                out.append(f'<Placemark><name>Pin</name><Point><coordinates>'
                           f'{pin[1]},{pin[0]}</coordinates></Point></Placemark>')
        label = f"S{sn} {club}" + (f" {round(dist)}y" if dist else "")
        if end and None not in end:
            out.append(f'<Placemark><name>{label}</name><styleUrl>#{cat}</styleUrl>'
                       f'<LineString><coordinates>{start[1]},{start[0]} {end[1]},{end[0]}'
                       f'</coordinates></LineString></Placemark>')
    if cur_hole is not None:
        out.append('</Folder>')
    out.append('</Document></kml>')
    return "\n".join(out)


def build_geojson(detail: dict, rid):
    feats = []
    for hid, sn, club, dist, cat, start, end, pin in round_features(detail):
        if end and None not in end:
            feats.append({"type": "Feature", "geometry": {"type": "LineString",
                          "coordinates": [[start[1], start[0]], [end[1], end[0]]]},
                          "properties": {"hole": hid, "shot": sn, "club": club,
                                         "distance_yd": dist, "category": cat}})
    return {"type": "FeatureCollection", "round_id": rid, "features": feats}


def main():
    os.makedirs(MAPS_DIR, exist_ok=True)
    files = sorted(glob.glob(os.path.join(CACHE_ROUNDS, "*.json")))
    files = [f for f in files if not f.endswith(("_ach.json", "_dash.json", "_analytics.json"))]
    if not files:
        sys.exit(f"No cached rounds in {CACHE_ROUNDS}. Run pull_arccos.py first.")

    csv_rows = []
    n_rounds = 0
    for f in files:
        detail = json.load(open(f, encoding="utf-8"))
        rid = detail.get("roundId") or os.path.basename(f)[:-5]
        course = detail.get("courseName") or "course"
        date = (detail.get("startTime") or "")[:10]
        kml = build_kml(detail, rid, course, date)
        if kml.count("LineString") == 0:
            continue
        n_rounds += 1
        with open(os.path.join(MAPS_DIR, f"round_{rid}.kml"), "w", encoding="utf-8") as o:
            o.write(kml)
        with open(os.path.join(MAPS_DIR, f"round_{rid}.geojson"), "w", encoding="utf-8") as o:
            json.dump(build_geojson(detail, rid), o, indent=2)
        for hid, sn, club, dist, cat, start, end, pin in round_features(detail):
            csv_rows.append({"round_id": rid, "date": date, "course": course, "hole": hid,
                             "shot": sn, "club": club, "distance_yd": dist, "category": cat,
                             "start_lat": start[0], "start_lng": start[1],
                             "end_lat": end[0] if end else "", "end_lng": end[1] if end else "",
                             "pin_lat": pin[0], "pin_lng": pin[1]})
    cols = ["round_id", "date", "course", "hole", "shot", "club", "distance_yd", "category",
            "start_lat", "start_lng", "end_lat", "end_lng", "pin_lat", "pin_lng"]
    with open(os.path.join(MAPS_DIR, "shots_geo.csv"), "w", newline="", encoding="utf-8") as o:
        w = csv.DictWriter(o, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in csv_rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    print(f"Wrote {n_rounds} round map(s) + shots_geo.csv ({len(csv_rows)} shots) -> {MAPS_DIR}")
    print("Open a round_<id>.kml in Google Earth -> satellite -> export/print PDF.")


if __name__ == "__main__":
    main()
