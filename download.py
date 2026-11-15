"""Download hotspots from the FIRMS Area API in 5-day chunks.

Needs a free MAP_KEY: https://firms.modaps.eosdis.nasa.gov/api/map_key/
The API is rate limited, so for MODIS history (2000-) it is much faster to
request an archive download on the FIRMS site and drop the CSVs into
data/raw/MODIS_SP/ (and data/raw/VIIRS_SNPP_SP/ etc.). Check the API docs for
current source names and date limits before a long run.
"""
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from config import RAW_DIR

BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"


def fetch_chunk(map_key, source, bbox, start, days=5, retries=3):
    west, south, east, north = bbox
    url = f"{BASE}/{map_key}/{source}/{west},{south},{east},{north}/{days}/{start:%Y-%m-%d}"
    for attempt in range(retries):
        r = requests.get(url, timeout=60)
        if r.ok:
            return r.text
        time.sleep(5 * (attempt + 1))
    r.raise_for_status()


def download_range(map_key, source, bbox, start: date, end: date, out_dir=RAW_DIR, pause=0.5):
    folder = Path(out_dir) / source
    folder.mkdir(parents=True, exist_ok=True)
    d = start
    while d <= end:
        f = folder / f"{source}_{d:%Y%m%d}.csv"
        if not f.exists():                       # resumable
            text = fetch_chunk(map_key, source, bbox, d)
            if not text.lstrip().startswith("latitude"):
                # FIRMS answers with plain text (not a CSV) on a bad key or when the rate limit is hit
                raise RuntimeError(f"FIRMS did not return a CSV for {source} {d}: {text.strip()[:200]!r}")
            f.write_text(text)       # header-only = no fires that window; saved so the run can resume
            time.sleep(pause)
        d += timedelta(days=5)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--key", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    a = p.parse_args()
    download_range(a.key, a.source, a.bbox, date.fromisoformat(a.start), date.fromisoformat(a.end))
