"""Check your fire_*.csv files BEFORE running the pipeline:   python inspect_raw.py [folder]"""
import sys
from pathlib import Path

import pandas as pd

from clean import WANTED, _SAT_COL, _SAT_NAME
from config import RAW_DIR

folder = Path(sys.argv[1]) if len(sys.argv) > 1 else RAW_DIR
files = sorted(folder.rglob("*.csv"))
if not files:
    sys.exit(f"No CSV files under {folder}. Put your fire_*.csv files there.")

rows, problems = [], []
for f in files:
    head = pd.read_csv(f, nrows=3)
    missing = {"latitude", "longitude", "acq_date", "acq_time", "confidence", "frp"} - set(head.columns)
    if missing:
        problems.append(f"{f.name}: missing columns {sorted(missing)}")
    lo, hi, n, lat, lon, sats, inst = None, None, 0, [None, None], [None, None], set(), None
    for c in pd.read_csv(f, usecols=lambda col: col in WANTED, chunksize=1_000_000, dtype={"satellite": str}):
        n += len(c)
        lo = c.acq_date.min() if lo is None else min(lo, c.acq_date.min())
        hi = c.acq_date.max() if hi is None else max(hi, c.acq_date.max())
        lat = [c.latitude.min() if lat[0] is None else min(lat[0], c.latitude.min()),
               c.latitude.max() if lat[1] is None else max(lat[1], c.latitude.max())]
        lon = [c.longitude.min() if lon[0] is None else min(lon[0], c.longitude.min()),
               c.longitude.max() if lon[1] is None else max(lon[1], c.longitude.max())]
        sats |= set(c["satellite"].astype(str).unique()) if "satellite" in c else set()
        inst = c["instrument"].iloc[0] if "instrument" in c and inst is None else inst
    unknown = [s for s in sats if s.upper() not in _SAT_COL and not any(k.lower() in f.name.lower() for k, _ in _SAT_NAME)]
    if unknown:
        problems.append(f"{f.name}: unrecognised satellite codes {unknown} (add to clean._SAT_COL)")
    rows.append(dict(file=f.name, instrument=inst, satellites=",".join(sorted(sats)), rows=n,
                     first=lo, last=hi, lon=f"{lon[0]:.1f}..{lon[1]:.1f}", lat=f"{lat[0]:.1f}..{lat[1]:.1f}"))

t = pd.DataFrame(rows)
pd.set_option("display.width", 200, "display.max_columns", 20)
print(t.to_string(index=False))

print("\n--- coverage check ---")
t["sensor"] = [("MODIS" if (str(i).upper().startswith("MODIS") or "m-c" in f.lower()) else "VIIRS") for i, f in zip(t.instrument, t.file)]
for s, g in t.groupby("sensor"):
    print(f"{s}: {g['first'].min()} -> {g['last'].max()}   ({g.rows.sum():,} rows)")
m, v = t[t.sensor == "MODIS"], t[t.sensor == "VIIRS"]
if m.empty or v.empty:
    problems.append("Need BOTH MODIS and VIIRS files.")
else:
    if m["first"].min() > "2005-01-01":
        problems.append(f"MODIS starts {m['first'].min()}: the 2000s history is the point of the challenge.")
    if v["first"].min() > "2013-12-31":
        problems.append("VIIRS SNPP should start in 2012 to give a long overlap with MODIS.")
    overlap_start, overlap_end = max(m["first"].min(), v["first"].min()), min(m["last"].max(), v["last"].max())
    months = (pd.Timestamp(overlap_end) - pd.Timestamp(overlap_start)).days / 30
    print(f"Overlap MODIS/VIIRS: {overlap_start} -> {overlap_end} (~{months:.0f} months; want >= 24)")
    if months < 12:
        problems.append("Overlap under 12 months: harmonization cannot be calibrated.")
print("\n" + ("PROBLEMS:\n- " + "\n- ".join(problems) if problems else "Looks good. Next: python run_pipeline.py --indonesia"))
