"""Aggregate cleaned hotspots to cell x sensor x month."""
import pandas as pd

from config import GRID_DEG, PERSISTENT_FRAC, cell_id_from_lonlat


def aggregate_monthly(df: pd.DataFrame, deg: float = GRID_DEG) -> pd.DataFrame:
    """Metrics per cell-month:
       n_hotspots  raw detections (inflated by extra satellites)
       fire_days   distinct dates with >=1 detection (robust to duplicate satellites) <- preferred
       frp_sum     summed fire radiative power (MW)
    """
    df = df.assign(cell_id=cell_id_from_lonlat(df.longitude, df.latitude, deg))
    return (df.groupby(["cell_id", "sensor", "month"])
              .agg(n_hotspots=("frp", "size"),
                   fire_days=("acq_date", "nunique"),
                   frp_sum=("frp", "sum"))
              .reset_index())


def sensor_month_meta(df: pd.DataFrame) -> pd.DataFrame:
    """How many satellites of each sensor reported in each month (sampling effort).
    VIIRS goes 1 -> 2 -> 3 satellites over time; ignoring that creates a fake trend."""
    return (df.groupby(["sensor", "month"]).satellite.nunique()
              .rename("n_sats").reset_index())


def drop_persistent(agg: pd.DataFrame, frac: float = PERSISTENT_FRAC):
    """Remove cells that burn in most months of the record (gas flares, industry)."""
    total = agg.groupby("sensor").month.nunique().rename("total")
    seen = agg.groupby(["cell_id", "sensor"]).month.nunique().rename("seen").reset_index()
    seen = seen.merge(total, on="sensor")
    bad = set(seen.loc[seen.seen / seen.total > frac, "cell_id"])
    return agg[~agg.cell_id.isin(bad)].copy(), sorted(bad)
