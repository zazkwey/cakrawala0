"""Fake data so both of you can work before real FIRMS data is downloaded.

  python synthetic.py harmonized   -> data/processed/harmonized_monthly.parquet  (for the app)
  python synthetic.py raw          -> data/raw/<SOURCE>/*.csv in real FIRMS format (for the pipeline)
"""
import sys

import numpy as np
import pandas as pd

from config import PROCESSED_DIR, RAW_DIR, METRICS, cell_id_from_lonlat, lonlat_from_cell_id

BBOX = (110.0, -4.0, 113.0, -1.0)           # Central Kalimantan-ish
SATCODE = {"VIIRS_SNPP_SP": "N", "VIIRS_NOAA20_SP": "N20"}
FILECODE = {"MODIS_SP": "M-C61", "VIIRS_SNPP_SP": "SV-C2", "VIIRS_NOAA20_SP": "J1V-C2"}
EL_NINO = {2015: 4.0, 2019: 3.0, 2023: 3.0, 2009: 2.0}


def _rate(dates):
    doy = dates.dayofyear.to_numpy()
    seasonal = 1 + 3 * np.exp(-((doy - 250) / 35.0) ** 2)
    year = np.array([EL_NINO.get(y, 1.0) for y in dates.year])
    return 6 * seasonal * year


def make_raw(seed=0):
    rng = np.random.default_rng(seed)
    centers = np.column_stack([rng.uniform(BBOX[0], BBOX[2], 40), rng.uniform(BBOX[1], BBOX[3], 40)])
    plan = {  # source: (first year, last year, detect prob per event, hotspots per detection)
        "MODIS_SP": (2008, 2024, 0.30, 1), "VIIRS_SNPP_SP": (2012, 2024, 0.60, 2),
        "VIIRS_NOAA20_SP": (2018, 2024, 0.60, 2)}
    dates_all = pd.date_range("2008-01-01", "2024-11-14", freq="D")   # ends mid-month on purpose: tests the partial-month path
    n_events = rng.poisson(_rate(dates_all))
    for source, (y0, y1, p, k) in plan.items():
        sats = ["Terra", "Aqua"] if source.startswith("MODIS") else [None]
        for year in range(y0, y1 + 1):
            rows = []
            for d, n in zip(dates_all[dates_all.year == year], n_events[dates_all.year == year]):
                if n == 0:
                    continue
                for sat in sats:
                    det = rng.random(n) < p
                    m = int(det.sum() * k)
                    if m == 0:
                        continue
                    c = centers[rng.integers(0, 40, m)] + rng.normal(0, 0.12, (m, 2))
                    hour = 10 if sat == "Terra" else 13
                    rows.append(pd.DataFrame({
                        "latitude": c[:, 1].round(5), "longitude": c[:, 0].round(5),
                        "acq_date": d.strftime("%Y-%m-%d"), "acq_time": hour * 100 + rng.integers(0, 59, m),
                        "satellite": sat or SATCODE[source], "instrument": source.split("_")[0],
                        "confidence": rng.integers(20, 100, m).astype(str) if sat else rng.choice(list("lnnhh"), m),
                        "frp": rng.gamma(2.0, 8.0, m).round(1), "daynight": "D", "type": 0}))
                # a gas flare: fires almost every day (should be removed by drop_persistent)
                if rng.random() < 0.9:
                    for sat in sats:
                        rows.append(pd.DataFrame({
                            "latitude": [-2.51], "longitude": [111.51], "acq_date": d.strftime("%Y-%m-%d"),
                            "acq_time": [1330], "satellite": sat or SATCODE[source], "instrument": source.split("_")[0],
                            "confidence": ["90" if sat else "h"], "frp": [30.0], "daynight": "D", "type": 0}))
            if rows:
                RAW_DIR.mkdir(parents=True, exist_ok=True)
                pd.concat(rows).to_csv(RAW_DIR / f"fire_archive_{FILECODE[source]}_{year}.csv", index=False)
    print("raw synthetic written to", RAW_DIR)


def make_harmonized(seed=0, cell=0.1):
    """Same schema as harmonize.harmonize() output."""
    rng = np.random.default_rng(seed)
    gx, gy = np.meshgrid(np.arange(BBOX[0], BBOX[2], cell), np.arange(BBOX[1], BBOX[3], cell))
    lon, lat = gx.ravel() + cell / 2, gy.ravel() + cell / 2
    weight = np.exp(-(((lon - 111.5) / 0.8) ** 2 + ((lat + 2.5) / 0.8) ** 2)) + 0.05
    months = pd.date_range("2003-01-01", "2026-10-01", freq="MS")
    frames = []
    for mth in months:
        lam = weight * 0.4 * (1 + 3 * np.exp(-((mth.month - 8.5) / 1.3) ** 2)) * EL_NINO.get(mth.year, 1.0)
        n = rng.poisson(lam)
        keep = n > 0
        fd = np.minimum(n[keep], 28).astype(float)
        f = pd.DataFrame({"lon": lon[keep], "lat": lat[keep], "month": mth, "fire_days": fd,
                          "n_hotspots": fd * 1.4, "frp_sum": fd * 22.0,
                          "source": "MODIS" if mth < pd.Timestamp("2023-01-01") else "VIIRS_mapped"})
        frames.append(f)
    df = pd.concat(frames, ignore_index=True)
    df["cell_id"] = cell_id_from_lonlat(df.lon, df.lat)
    unc = np.where(df.source == "MODIS", 0.0, 0.2)
    for m in METRICS:
        df[m + "_lo"], df[m + "_hi"] = df[m] * (1 - unc), df[m] * (1 + unc)
    cols = ["cell_id", "lon", "lat", "month", "source"] + [c for m in METRICS for c in (m, m + "_lo", m + "_hi")]
    out = PROCESSED_DIR / "harmonized_monthly.parquet"
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df[cols].to_parquet(out)
    print("harmonized synthetic written to", out, len(df), "rows")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "harmonized"
    make_raw() if what == "raw" else make_harmonized()
