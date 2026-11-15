"""raw FIRMS CSVs -> cleaned -> monthly cell aggregates -> harmonized parquet + hotspot files."""
import argparse
import json

import pandas as pd

from clean import QC, load_all
from config import (INDONESIA_BBOX, METRICS, INTERIM_DIR, MODIS_LAST_MONTH, OVERLAP_END, OVERLAP_START,
                    PROCESSED_DIR, cell_id_from_lonlat, lonlat_from_cell_id)
from grid import aggregate_monthly, drop_persistent, sensor_month_meta
from harmonize import backtest, harmonize


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    p.add_argument("--indonesia", action="store_true", help="shortcut for the Indonesia bounding box")
    p.add_argument("--modis-last", default=MODIS_LAST_MONTH, help="e.g. 2022-12-01 to simulate MODIS retirement")
    p.add_argument("--backtest", metavar="CUTOFF", help="e.g. 2022-12-01 (defaults to --modis-last)")
    p.add_argument("--demo", action="store_true", help="shortcut for --modis-last 2022-12-01 (simulate MODIS retirement + validate)")
    args = p.parse_args()
    if args.demo and not args.modis_last:
        args.modis_last = "2022-12-01"
    args.backtest = args.backtest or args.modis_last      # always validate the simulated retirement
    INTERIM_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    bbox = INDONESIA_BBOX if args.indonesia else args.bbox
    print("loading files...")
    clean = load_all(bbox=bbox)
    print(f"cleaned hotspots: {len(clean):,}")
    qc = {"hotspots_after_cleaning": int(len(clean))}

    # last month for which every active satellite has data through month-end (later months are partial)
    sat_max = clean.groupby(["sensor", "satellite"]).acq_date.max()
    newest = pd.Timestamp(sat_max.max())
    active = sat_max[pd.to_datetime(sat_max) >= newest - pd.Timedelta(days=60)]
    cutoff = pd.Timestamp(active.min())
    last_complete = cutoff.to_period("M").to_timestamp() if cutoff == cutoff + pd.offsets.MonthEnd(0) \
        else (cutoff.to_period("M") - 1).to_timestamp()

    meta = sensor_month_meta(clean)
    agg = aggregate_monthly(clean)
    agg, dropped = drop_persistent(agg)
    print(f"dropped {len(dropped)} persistent (flare/industrial) cells")
    qc["persistent_cells_dropped"] = len(dropped)
    agg.to_parquet(INTERIM_DIR / "monthly_by_sensor.parquet")
    meta.to_parquet(INTERIM_DIR / "sensor_month_meta.parquet")

    # ---- hotspot files for the CSV export: same cells and months as the calendar, split by year
    hot = clean                      # includes the current partial month: the app uses it for 'Right now' and CSV
    hot = hot[~pd.Series(cell_id_from_lonlat(hot.longitude, hot.latitude), index=hot.index).isin(dropped)]
    qc["hotspots_exported"] = int(len(hot))
    hot = hot[["sensor", "satellite", "latitude", "longitude", "acq_date", "acq_time", "conf", "daynight", "frp"]].copy()
    hot = hot.rename(columns={"conf": "confidence"})
    hot[["latitude", "longitude"]] = hot[["latitude", "longitude"]].astype("float32")
    hot["sensor"], hot["satellite"] = hot["sensor"].astype("category"), hot["satellite"].astype("category")
    hot["year"] = hot["acq_date"].str[:4].astype("int16")
    hot["acq_date"] = pd.to_datetime(hot["acq_date"]).dt.date
    import shutil
    shutil.rmtree(PROCESSED_DIR / "hotspots", ignore_errors=True)
    shutil.rmtree(PROCESSED_DIR / "hotspots.parquet", ignore_errors=True)
    (PROCESSED_DIR / "hotspots.parquet").unlink(missing_ok=True)
    hot.sort_values("acq_date").to_parquet(PROCESSED_DIR / "hotspots", partition_cols=["year"],
                                            compression="zstd", index=False)
    del hot

    summary = None
    if args.backtest:
        summary = {m: backtest(agg, meta, args.backtest, m)[1] for m in METRICS}
        for m, s in summary.items():
            print(f"backtest {m}: bias {s['bias_ratio']:.2f}, corr {s['corr_log']:.2f}, 95% band coverage {s['coverage_95']:.0%}")

    if not args.modis_last:
        print("\n*** WARNING: no --modis-last given, so the output is MODIS only (nothing is mapped from VIIRS).\n"
              "    Pass e.g. --modis-last 2022-12-01 to simulate the MODIS retirement and use the harmonization. ***\n")
    out = harmonize(agg, meta, OVERLAP_START, OVERLAP_END, args.modis_last)
    out.to_parquet(PROCESSED_DIR / "harmonized_monthly.parquet")
    modis_last_month = None
    cont = PROCESSED_DIR / "continuity_modis.parquet"
    cont.unlink(missing_ok=True)
    if args.modis_last:
        # the REAL MODIS after the simulated retirement, kept aside so the app can show it next to the mapped VIIRS
        modis_last_month = pd.Timestamp(args.modis_last).strftime("%Y-%m-%d")
        real = agg[(agg.sensor == "MODIS") & (agg.month > pd.Timestamp(args.modis_last)) & (agg.month <= last_complete)]
        lon, lat = lonlat_from_cell_id(real.cell_id)
        real.assign(lon=lon, lat=lat).drop(columns="sensor").to_parquet(cont)
    print(f"harmonized rows: {len(out):,} -> {PROCESSED_DIR / 'harmonized_monthly.parquet'}")

    (PROCESSED_DIR / "meta.json").write_text(json.dumps({
        "last_complete_month": last_complete.strftime("%Y-%m-%d"),
        "data_end_by_satellite": {f"{s}/{t}": d for (s, t), d in sat_max.items()},
        "backtest": summary, "backtest_cutoff": (args.backtest or "")[:7],
        "harmonization_active": bool(args.modis_last), "modis_last_month": modis_last_month, "qc": {**dict(sorted(QC.items())), **qc}}, indent=2))


if __name__ == "__main__":
    main()
