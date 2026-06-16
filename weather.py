"""
weather.py — external weather enrichment for Arccos rounds.

Arccos carries NO weather data. This module fetches historical reanalysis
weather from Open-Meteo's free archive API (no API key required, stdlib only)
by course lat/lng + round date, picking the hourly slot nearest the round's
mid-time (mean of startTime + endTime, UTC).

IMPORTANT CAVEATS:
  - Source: Open-Meteo historical reanalysis (ERA5 grid), NOT Arccos.
  - Reanalysis is approximate — nearest grid cell (~9 km) + nearest UTC hour.
  - Data represents modelled atmospheric conditions, not on-course observations.
  - Fetch is always graceful: any failure (network, HTTP error, missing hour,
    malformed response) returns {} so the pull is never broken by weather.
  - Results are cached per (lat, lng, date) under <cache_dir>/_cache_weather/
    to keep re-runs idempotent (same contract as the rest of the pull).
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from typing import Any, Optional

_ARCHIVE_URL = (
    "https://archive-api.open-meteo.com/v1/archive"
    "?latitude={lat}&longitude={lng}"
    "&start_date={date}&end_date={date}"
    "&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,weather_code"
    "&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=UTC"
)

_TIMEOUT = 15  # seconds; graceful on any failure


# ---------------------------------------------------------------------------
# WMO weather-code -> short text (table 4677 subset used by Open-Meteo)
# ---------------------------------------------------------------------------

def weather_code_text(code: int) -> str:
    """Map WMO weather interpretation code to short human-readable text."""
    if code == 0:
        return "Clear"
    if code == 1:
        return "Mostly clear"
    if code == 2:
        return "Partly cloudy"
    if code == 3:
        return "Overcast"
    if code in (45, 48):
        return "Fog"
    if 51 <= code <= 57:
        return "Drizzle"
    if 61 <= code <= 67:
        return "Rain"
    if 71 <= code <= 77:
        return "Snow"
    if 80 <= code <= 82:
        return "Showers"
    if 95 <= code <= 99:
        return "Thunderstorm"
    return f"Code {code}"


# ---------------------------------------------------------------------------
# 8-point compass from bearing degrees
# ---------------------------------------------------------------------------

_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def cardinal(deg: float) -> str:
    """Convert wind direction in degrees (0=N, clockwise) to 8-point cardinal."""
    idx = int((deg % 360 + 22.5) / 45) % 8
    return _COMPASS[idx]


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: str, lat: float, lng: float, date: str) -> str:
    key = f"{round(lat, 3)},{round(lng, 3)},{date}"
    # Replace characters unsafe for filenames (comma is fine on Linux but be safe).
    safe = key.replace(",", "_")
    return os.path.join(cache_dir, "_cache_weather", f"{safe}.json")


def _load_cache(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _save_cache(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:  # noqa: BLE001
        pass  # cache write failure is non-fatal


# ---------------------------------------------------------------------------
# Core fetch
# ---------------------------------------------------------------------------

def fetch_round_weather(
    lat: float,
    lng: float,
    date: str,
    mid_hour_utc: int,
    cache_dir: Optional[str] = None,
) -> dict:
    """Fetch historical weather for a round from Open-Meteo archive API.

    Args:
        lat:          Course latitude (float).
        lng:          Course longitude (float).
        date:         Round date as "YYYY-MM-DD".
        mid_hour_utc: UTC hour nearest the round mid-time (0-23).
        cache_dir:    Root store directory; weather cache goes under
                      <cache_dir>/_cache_weather/. Pass None to skip caching.

    Returns:
        {"temp_f": float, "wind_mph": float, "wind_dir_deg": int,
         "wind_dir": str, "weather": str}
        or {} on any failure (network, HTTP error, missing hour, malformed data).

    This function NEVER raises; all errors are swallowed and {} returned so the
    Arccos pull is never broken by weather unavailability.
    """
    try:
        cache_path: Optional[str] = (
            _cache_path(cache_dir, lat, lng, date) if cache_dir else None
        )

        # Cache hit.
        if cache_path:
            cached = _load_cache(cache_path)
            if cached is not None:
                return cached

        url = _ARCHIVE_URL.format(lat=lat, lng=lng, date=date)
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload: Any = json.loads(resp.read().decode("utf-8"))

        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        temps = hourly.get("temperature_2m") or []
        winds = hourly.get("wind_speed_10m") or []
        dirs = hourly.get("wind_direction_10m") or []
        codes = hourly.get("weather_code") or []

        # Find the index whose UTC hour matches mid_hour_utc.
        idx: Optional[int] = None
        for i, t in enumerate(times):
            # t is like "2026-06-14T13:00" — extract the hour component.
            try:
                hour = int(t[11:13])
            except (IndexError, ValueError):
                continue
            if hour == mid_hour_utc:
                idx = i
                break

        # Fallback: if exact hour not found, pick closest available.
        if idx is None and times:
            best_dist = 25
            for i, t in enumerate(times):
                try:
                    hour = int(t[11:13])
                except (IndexError, ValueError):
                    continue
                dist = abs(hour - mid_hour_utc)
                if dist < best_dist:
                    best_dist = dist
                    idx = i

        if idx is None:
            return {}

        def _safe_val(lst: list, i: int) -> Optional[Any]:
            return lst[i] if i < len(lst) else None

        temp = _safe_val(temps, idx)
        wind = _safe_val(winds, idx)
        wdir = _safe_val(dirs, idx)
        code = _safe_val(codes, idx)

        if temp is None or wind is None or wdir is None or code is None:
            return {}

        result = {
            "temp_f": round(float(temp), 1),
            "wind_mph": round(float(wind), 1),
            "wind_dir_deg": int(round(float(wdir))),
            "wind_dir": cardinal(float(wdir)),
            "weather": weather_code_text(int(round(float(code)))),
        }

        if cache_path:
            _save_cache(cache_path, result)

        return result

    except Exception:  # noqa: BLE001
        # Any failure is graceful — never break the pull.
        return {}
