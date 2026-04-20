# dji-srt-parser

Extract GPS location from DJI drone SRT subtitle files. Returns structured location data with reverse geocoding via OpenStreetMap (free, no API key needed).

## Why

DJI drones write GPS telemetry into `.SRT` files frame-by-frame alongside video. But the format varies across drone models and firmware versions — some use `[latitude: ...]`, others use `GPS(...)`, others use `LATITUDE: ...`. This parser handles all of them, takes the median GPS fix across all frames (more stable than first/last), and reverse-geocodes to give you a clean location name.

## Install

```bash
pip install requests
```

No other dependencies. Python 3.10+.

## Usage

### CLI

```bash
python3 dji_srt_parser.py DJI_0042.SRT
```

Output:
```json
{
  "lat": 23.54321,
  "lon": 58.98765,
  "alt_m": 145.0,
  "country": "Oman",
  "region": "Ash Sharqiyah",
  "city": "Wadi Shab",
  "display": "Wadi Shab, Oman",
  "display_full": "Wadi Shab, Ash Sharqiyah, Oman",
  "frame_count": 847
}
```

### As a library

```python
from dji_srt_parser import parse_srt_file
from pathlib import Path

loc = parse_srt_file(Path("DJI_0042.SRT"))
print(f"Shot in {loc['display']} at {loc['alt_m']:.0f}m")
```

## Supported drones

Tested with SRT files from:

- DJI Mini 2, Mini 3, Mini 3 Pro
- DJI Air 2S, Air 3
- DJI Mavic 3
- DJI FPV, Avata

Should work with any DJI drone that writes GPS to SRT files.

## How it works

1. Tries multiple regex patterns against the SRT text (handles firmware variations)
2. Extracts all GPS fixes across every frame
3. Takes median lat/lon/alt (robust against GPS jitter and cold-start drift)
4. Reverse-geocodes via Nominatim (OpenStreetMap) — free, no key required
5. Returns structured location dict

## Notes

- Nominatim has a 1 req/sec rate limit. The tool respects this with exponential backoff on failures.
- If the drone had no GPS fix (indoor flights, GPS denied environments), returns `None`.
- Altitude is barometric (relative to takeoff) on most DJI drones, not MSL.
