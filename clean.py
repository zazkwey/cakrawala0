"""Read FIRMS CSVs (fire_archive_*.csv / fire_nrt_*.csv, MODIS and VIIRS) onto one schema.

Everything is auto-detected from the files: drop them anywhere under data/raw/.
  sensor     <- `instrument` column (MODIS / VIIRS)
  satellite  <- filename code (SV-C2, J1V-C2, J2V-C2) or `satellite` column
  product    <- "NRT" if the path contains "nrt", otherwise "SP" (standard/archive)
"""
from collections import Counter
from pathlib import Path

import pandas as pd

from config import MIN_CONFIDENCE, RAW_DIR

WANTED = {"latitude", "longitude", "acq_date", "acq_time", "satellite",
          "instrument", "confidence", "frp", "daynight", "type"}
QC = Counter()     # rows removed at each step; run_pipeline writes it to meta.json
_CONF_RANK = {"low": 0, "nominal": 1, "high": 2}
_SAT_COL = {"N": "SNPP", "NPP": "SNPP", "SNPP": "SNPP", "S-NPP": "SNPP",
            "N20": "NOAA20", "1": "NOAA20", "J1": "NOAA20", "NOAA-20": "NOAA20", "NOAA20": "NOAA20",
            "N21": "NOAA21", "2": "NOAA21", "J2": "NOAA21", "NOAA-21": "NOAA21", "NOAA21": "NOAA21",
            "T": "Terra", "TERRA": "Terra", "A": "Aqua", "AQUA": "Aqua"}
_SAT_NAME = [("SV-C2", "SNPP"), ("J1V", "NOAA20"), ("J2V", "NOAA21")]


def _read(path: Path, bbox=None, chunk=1_000_000) -> pd.DataFrame:
    parts = []
    for c in pd.read_csv(path, usecols=lambda col: col in WANTED, dtype={"acq_time": str, "confidence": str,
                                                                      "satellite": str}, chunksize=chunk):
        QC["1_rows_read"] += len(c)
        if bbox is not None:
            w, s, e, n = bbox
            c = c[c.longitude.between(w, e) & c.latitude.between(s, n)]
        QC["2_rows_inside_bbox"] += len(c)
        parts.append(c)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _confidence_class(s: pd.Series, sensor: str) -> pd.Series:
    if sensor == "MODIS":                      # numeric 0-100
        v = pd.to_numeric(s, errors="coerce")
        return pd.cut(v, [-1, 29, 79, 100], labels=["low", "nominal", "high"]).astype(object)
    return s.astype(str).str.lower().str[0].map({"l": "low", "n": "nominal", "h": "high"})


def standardize(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    if df.empty:
        return df
    name = Path(path).name.lower()   # filename only, so a folder called "nrt" cannot confuse it
    inst = str(df["instrument"].iloc[0]).upper() if "instrument" in df else ""
    sensor = "MODIS" if inst.startswith("MODIS") or "m-c" in name else "VIIRS"

    if "type" in df.columns:                   # 0 = vegetation fire; drop volcano/static/offshore
        n0 = len(df)
        df = df[pd.to_numeric(df["type"], errors="coerce").fillna(0) == 0]
        QC["removed_not_vegetation_fire_type"] += n0 - len(df)

    code = next((sat for key, sat in _SAT_NAME if key.lower() in name), None)
    sat_col = df["satellite"].astype(str).str.upper().map(_SAT_COL).fillna(df["satellite"].astype(str))
    df = df.assign(sensor=sensor, satellite=code if (code and sensor == "VIIRS") else sat_col,
                   product="NRT" if "nrt" in name else "SP")

    df = df.assign(conf=_confidence_class(df["confidence"], sensor))
    n0 = len(df)
    df = df[df["conf"].map(_CONF_RANK).fillna(0) >= _CONF_RANK[MIN_CONFIDENCE]]
    QC["removed_low_confidence"] += n0 - len(df)

    t = df["acq_time"].astype(str).str.zfill(4)
    dt = pd.to_datetime(df["acq_date"] + " " + t.str[:2] + ":" + t.str[2:], format="%Y-%m-%d %H:%M")
    df = df.assign(acq_time=t.astype("int16"),
                   conf=pd.Categorical(df["conf"], categories=["nominal", "high"]),
                   daynight=pd.Categorical(df["daynight"], categories=["D", "N"]),
                   acq_date=dt.dt.strftime("%Y-%m-%d"),
                   month=dt.dt.to_period("M").dt.to_timestamp(),
                   local_hour=((dt.dt.hour + dt.dt.minute / 60 + df["longitude"] / 15) % 24).astype("float32"),
                   frp=pd.to_numeric(df["frp"], errors="coerce").fillna(0.0).astype("float32"))
    keep = ["sensor", "satellite", "product", "latitude", "longitude", "acq_date", "month", "frp", "local_hour", "acq_time", "conf", "daynight"]
    for c in ("sensor", "satellite", "product"):
        df[c] = df[c].astype("category")
    return df[keep]


def load_all(raw_dir: Path = RAW_DIR, bbox=None) -> pd.DataFrame:
    """Load every CSV under raw_dir (recursive), both sensors. NRT rows are kept only for
    dates after the last standard-processing date of the same satellite."""
    files = sorted(Path(raw_dir).rglob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files found under {raw_dir}")
    parts = []
    for f in files:
        d = standardize(_read(f, bbox), f)
        if not d.empty:
            parts.append(d)
            print(f"  {f.name}: {len(d):,} rows kept")
    df = pd.concat(parts, ignore_index=True)
    for c in ("sensor", "satellite", "product"):
        df[c] = df[c].astype(str)

    sp_last = df[df["product"] == "SP"].groupby(["sensor", "satellite"]).acq_date.max()
    key = list(zip(df.sensor, df.satellite))
    last = pd.Series(key).map(sp_last.to_dict()).fillna("0000-00-00").to_numpy()
    stale_nrt = (df["product"] == "NRT").to_numpy() & (df["acq_date"].to_numpy() <= last)
    QC["removed_nrt_already_in_archive"] += int(stale_nrt.sum())
    df = df[~stale_nrt]
    n0 = len(df)
    df = df.drop_duplicates(["satellite", "latitude", "longitude", "acq_date", "local_hour"]).reset_index(drop=True)
    QC["removed_duplicates"] += n0 - len(df)
    return df
