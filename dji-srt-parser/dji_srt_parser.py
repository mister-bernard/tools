#!/usr/bin/env python3
"""
dji_srt_parser.py — Extract GPS metadata from DJI drone SRT subtitle files.

DJI drones embed GPS, altitude, ISO, shutter speed, and other telemetry into
.SRT files alongside video recordings, one entry per frame. This tool extracts
the median GPS position (more stable than first/last frame against jitter),
reverse-geocodes it via Nominatim (free OpenStreetMap API, no key needed),
and returns a structured location dict.

Supported drone formats:
  - DJI Mini 2/3, Air 2S, Mavic 3 (bracketed style)
  - DJI Mini 3 Pro, Air 3, newer RC (GPS() prefix style)
  - DJI FPV / Avata

Usage:
    python3 dji_srt_parser.py <file.SRT>
    python3 dji_srt_parser.py flight-footage.SRT

As a library:
    from dji_srt_parser import parse_srt_file
    loc = parse_srt_file(Path("flight.SRT"))
    # -> {"lat": 23.54, "lon": 58.98, "alt_m": 145, "country": "Oman",
    #     "region": "Ash Sharqiyah", "city": "Wadi Shab", "display": "Wadi Shab, Oman"}
"""

import re
import json
import time
import statistics
import requests
from pathlib import Path
from typing import Optional

_PATTERNS = [
    # Old format: [latitude: 23.5456] [longitude: 58.9876] [altitude: 145.20]
    re.compile(r'\[latitude:\s*([-\d.]+)\].*?\[longitude:\s*([-\d.]+)\].*?\[altitude:\s*([-\d.]+)\]', re.IGNORECASE),
    # New format: GPS(23.5456, 58.9876, 145)
    re.compile(r'GPS\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)'),
    # Display format: LATITUDE: 23.5456, LONGITUDE: 58.9876
    re.compile(r'LATITUDE:\s*([-\d.]+).*?LONGITUDE:\s*([-\d.]+)', re.IGNORECASE),
    # Inline: lat/lon as separate tokens
    re.compile(r'lat[itude]*[:\s]+([-\d.]+)[,\s]+lon[gitude]*[:\s]+([-\d.]+)', re.IGNORECASE),
]
_ALT_PATTERN = re.compile(r'(?:altitude|alt)[:\s]+([-\d.]+)', re.IGNORECASE)


def _extract_coords(srt_text: str) -> list[tuple[float, float, float]]:
    coords = []
    for pat in _PATTERNS:
        matches = pat.findall(srt_text)
        for m in matches:
            try:
                lat, lon = float(m[0]), float(m[1])
                alt = float(m[2]) if len(m) > 2 else 0.0
                if abs(lat) < 0.001 and abs(lon) < 0.001:
                    continue
                coords.append((lat, lon, alt))
            except (ValueError, IndexError):
                continue
        if coords:
            break

    if coords and coords[0][2] == 0.0:
        alts = [float(m) for m in _ALT_PATTERN.findall(srt_text) if m]
        if alts:
            coords = [(lat, lon, statistics.median(alts)) for lat, lon, _ in coords]

    return coords


def _median_coord(coords: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    alts = [c[2] for c in coords]
    return statistics.median(lats), statistics.median(lons), statistics.median(alts)


def _reverse_geocode(lat: float, lon: float, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "zoom": 10},
                headers={"User-Agent": "dji-srt-parser/1.0"},
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  Reverse geocode failed: {e}")
    return {}


def parse_srt_file(srt_path: Path) -> Optional[dict]:
    """
    Parse a DJI SRT file and return a location dict, or None if no GPS data found.
    """
    srt_path = Path(srt_path)
    if not srt_path.exists():
        return None

    try:
        text = srt_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  Read error: {e}")
        return None

    coords = _extract_coords(text)
    if not coords:
        print(f"  No GPS data found in {srt_path.name}")
        return None

    lat, lon, alt = _median_coord(coords)
    print(f"  GPS median: {lat:.5f}, {lon:.5f}, {alt:.0f}m ({len(coords)} frames)")

    geo = _reverse_geocode(lat, lon)
    addr = geo.get("address", {})

    country = addr.get("country", "")
    region  = (addr.get("state") or addr.get("region") or
               addr.get("province") or addr.get("county") or "")
    city    = (addr.get("city") or addr.get("town") or addr.get("village") or
               addr.get("hamlet") or addr.get("suburb") or "")
    display_name = geo.get("display_name", "")

    parts_short = [p for p in [city, country] if p]
    parts_full  = [p for p in [city, region, country] if p]
    display       = ", ".join(parts_short) if parts_short else display_name[:60]
    display_full  = ", ".join(parts_full)  if parts_full  else display_name[:80]

    return {
        "lat":          lat,
        "lon":          lon,
        "alt_m":        alt,
        "country":      country,
        "region":       region,
        "city":         city,
        "display":      display,
        "display_full": display_full,
        "frame_count":  len(coords),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: dji_srt_parser.py <file.SRT>")
        sys.exit(1)
    result = parse_srt_file(Path(sys.argv[1]))
    print(json.dumps(result, indent=2))
