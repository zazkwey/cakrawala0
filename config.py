"""Central settings. Both halves of the project import from here."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("FIRMS_DATA_DIR", ROOT / "data"))   # override to test without touching real data
RAW_DIR = DATA / "raw"              # raw/<SOURCE>/*.csv  (folder name = FIRMS source)
INTERIM_DIR = DATA / "interim"      # cleaned + gridded monthly aggregates
PROCESSED_DIR = DATA / "processed"  # harmonized output (the app reads this)

# --- grid -------------------------------------------------------------------
GRID_DEG = 0.05      # ~5 km cells. Do NOT go finer than MODIS (1 km) -> false precision
REGION_DEG = 5.0     # coarse blocks used to stratify the calibration


INDONESIA_BBOX = (94.0, -11.5, 141.5, 6.5)   # W, S, E, N

# --- cleaning ---------------------------------------------------------------
MIN_CONFIDENCE = "nominal"   # drop "low" on both sensors
PERSISTENT_FRAC = 0.5        # cell with fire in > 50% of all months = flare/industrial -> drop

# --- harmonization ----------------------------------------------------------
METRICS = ["fire_days", "n_hotspots", "frp_sum"]
OVERLAP_START = "2013-01-01"  # first full year with SNPP
OVERLAP_END = None            # None = as late as both sensors exist
MODIS_LAST_MONTH = None       # None = last month present in MODIS data
REFERENCE_N_SATS_MODIS = 2    # emulate Terra + Aqua

# --- cell id helpers (shared by every module) -------------------------------
CELL_ID_MULT = 10_000          # cell_id = iy * CELL_ID_MULT + ix  (geo.py decodes with the same constant)
assert 360 / GRID_DEG < CELL_ID_MULT, "GRID_DEG too fine for CELL_ID_MULT: ids would collide"


def cell_id_from_lonlat(lon, lat, deg=GRID_DEG):
    import numpy as np
    ix = np.floor((np.asarray(lon) + 180.0) / deg).astype("int64")
    iy = np.floor((np.asarray(lat) + 90.0) / deg).astype("int64")
    return iy * CELL_ID_MULT + ix

def lonlat_from_cell_id(cell_id, deg=GRID_DEG):
    import numpy as np
    cid = np.asarray(cell_id).astype("int64")
    iy, ix = cid // CELL_ID_MULT, cid % CELL_ID_MULT
    return (ix + 0.5) * deg - 180.0, (iy + 0.5) * deg - 90.0   # lon, lat
