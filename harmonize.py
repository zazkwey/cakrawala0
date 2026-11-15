"""HARMONIZATION LOGIC  (friend's file).

Contract: harmonize(agg, meta) -> DataFrame with columns
    cell_id, lon, lat, month, source, then for each metric in config.METRICS:
    <metric>, <metric>_lo, <metric>_hi
where source in {"MODIS", "VIIRS_mapped"}. The app only depends on this schema,
so any method can be swapped in as long as the output keeps it.

Baseline method (replace/improve it):
  1. Normalise each sensor's monthly totals per satellite (n_sats).
  2. In the overlap period, ratio k = sum(MODIS per sat) / sum(VIIRS per sat),
     per 5-degree region x month-of-year (fallbacks: month-of-year, then global).
  3. MODIS-equivalent = VIIRS per sat * k * REFERENCE_N_SATS_MODIS.
  4. Uncertainty: empirical 95% band from leave-one-year-out cross-validation (see _cv_error_bands).
  5. Splice: MODIS up to MODIS_LAST_MONTH, mapped VIIRS afterwards.

Ideas to improve: stratify by land cover, match Aqua-day vs SNPP-day overpasses
only, log-space regression, quantile mapping, per-cell rather than per-region k.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import (METRICS, MODIS_LAST_MONTH, OVERLAP_END, OVERLAP_START,
                    REFERENCE_N_SATS_MODIS, REGION_DEG, lonlat_from_cell_id)


@dataclass
class RatioModel:
    k: pd.Series          # (region, moy)
    k_moy: pd.Series
    k_all: float
    sd_region: pd.Series
    sd_global: float


# ---------------------------------------------------------------- preparation
def _prep(agg, meta, sensor):
    d = agg[agg.sensor == sensor].merge(
        meta[meta.sensor == sensor][["month", "n_sats"]], on="month", how="left")
    lon, lat = lonlat_from_cell_id(d.cell_id)
    d["lon"], d["lat"] = lon, lat
    d["region"] = (np.floor(lat / REGION_DEG).astype(int) + 100) * 1000 + (np.floor(lon / REGION_DEG).astype(int) + 200)
    d["moy"] = d.month.dt.month
    return d


def overlap_months(meta, start=OVERLAP_START, end=OVERLAP_END):
    both = (meta[meta.n_sats > 0].groupby("month").sensor.nunique() == 2)
    months = both[both].index
    months = months[months >= pd.Timestamp(start)]
    if end is not None:
        months = months[months <= pd.Timestamp(end)]
    return months


# ---------------------------------------------------------------- fit / predict
def _ratio_tables(df, min_total=20.0):
    """df: region-month totals with columns m (MODIS per sat), v (VIIRS per sat), moy.
    Returns k per (region, month-of-year), k per month-of-year, one global k."""
    g = df.groupby(["region", "moy"])[["m", "v"]].sum()
    ok = (g.v > 0) & ((g.m + g.v) >= min_total)
    k = (g.m / g.v)[ok]
    gm = df.groupby("moy")[["m", "v"]].sum()
    k_moy = (gm.m / gm.v).replace([np.inf, 0], np.nan).dropna()
    k_all = float(df.m.sum() / max(df.v.sum(), 1e-9)) or 1.0
    return k, k_moy, k_all


def _lookup_k(k, k_moy, k_all, region, moy):
    key = pd.MultiIndex.from_arrays([region, moy])
    out = k.reindex(key).to_numpy()
    out = np.where(np.isnan(out), pd.Series(moy).map(k_moy).to_numpy(), out)
    return np.where(np.isnan(out), k_all, out)


def _cv_error_bands(df, n_bins=4, min_per_bin=40):
    """Leave-one-YEAR-out cross-validation inside the overlap period. For each held-out year we predict its
    region-months from the other years and keep the log error log((obs+1)/(pred+1)). Empirical 2.5% / 97.5%
    quantiles of that error, per size class of the predicted total, become the band. This captures month-level
    noise and the tendency to under/over-predict, which the old yearly-ratio spread did not."""
    errs = []
    for yr in sorted(df.year.unique()):
        tr, te = df[df.year != yr], df[df.year == yr]
        if len(tr) < 24 or te.empty:
            continue
        k, k_moy, k_all = _ratio_tables(tr)
        pred = te.v.to_numpy() * _lookup_k(k, k_moy, k_all, te.region.to_numpy(), te.moy.to_numpy())
        errs.append(pd.DataFrame({"pred": pred, "err": np.log((te.m.to_numpy() + 1) / (pred + 1))}))
    if not errs:
        return np.array([]), np.array([-0.7]), np.array([0.7])
    e = pd.concat(errs, ignore_index=True)
    n_bins = max(1, min(n_bins, len(e) // min_per_bin))
    edges = np.unique(np.quantile(e.pred, np.linspace(0, 1, n_bins + 1)[1:-1]))
    b = np.searchsorted(edges, e.pred.to_numpy())
    qlo = np.array([e.err[b == i].quantile(0.025) for i in range(len(edges) + 1)])
    qhi = np.array([e.err[b == i].quantile(0.975) for i in range(len(edges) + 1)])
    return edges, qlo, qhi


@dataclass
class RatioModel:
    k: pd.Series          # (region, moy)
    k_moy: pd.Series
    k_all: float
    edges: np.ndarray     # size classes of the predicted region-month total
    qlo: np.ndarray       # log-error quantiles per size class (from leave-one-year-out CV)
    qhi: np.ndarray


def fit_ratio(modis, viirs, metric, months, min_total=20.0) -> RatioModel:
    def per_region_month(d):
        d = d[d.month.isin(months)]
        return (d[metric] / d.n_sats).groupby([d.region, d.month]).sum()

    df = pd.concat([per_region_month(modis).rename("m"),
                    per_region_month(viirs).rename("v")], axis=1).fillna(0).reset_index()
    df["moy"], df["year"] = df.month.dt.month, df.month.dt.year
    k, k_moy, k_all = _ratio_tables(df, min_total)
    edges, qlo, qhi = _cv_error_bands(df)
    return RatioModel(k, k_moy, k_all, edges, qlo, qhi)


def predict(model: RatioModel, viirs, metric):
    k = _lookup_k(model.k, model.k_moy, model.k_all, viirs.region.to_numpy(), viirs.moy.to_numpy())
    est = viirs[metric].to_numpy() / viirs.n_sats.to_numpy() * k * REFERENCE_N_SATS_MODIS
    # band from the size class of the whole region-month (cell errors are treated as fully correlated)
    tot = pd.Series(est).groupby([viirs.region.to_numpy(), viirs.month.to_numpy()]).transform("sum").to_numpy()
    b = np.searchsorted(model.edges, tot)
    safe = np.maximum(tot, 1e-9)
    f_lo = np.minimum(np.maximum((tot + 1) * np.exp(model.qlo[b]) - 1, 0.0) / safe, 1.0)
    f_hi = np.maximum(((tot + 1) * np.exp(model.qhi[b]) - 1) / safe, 1.0)
    return est, est * f_lo, est * f_hi        # cell bands sum exactly to the region-month band


# ---------------------------------------------------------------- main entry
def harmonize(agg, meta, overlap_start=OVERLAP_START, overlap_end=OVERLAP_END,
              modis_last=MODIS_LAST_MONTH) -> pd.DataFrame:
    modis, viirs = _prep(agg, meta, "MODIS"), _prep(agg, meta, "VIIRS")
    months = overlap_months(meta, overlap_start, overlap_end)
    if len(months) < 12:
        raise ValueError(f"Only {len(months)} overlap months; need >= 12 to calibrate.")
    modis_last = pd.Timestamp(modis_last) if modis_last else modis.month.max()

    v = viirs[viirs.month > modis_last].copy()
    for metric in METRICS:
        model = fit_ratio(modis, viirs, metric, months)
        v[metric], v[metric + "_lo"], v[metric + "_hi"] = predict(model, v, metric)
    v["source"] = "VIIRS_mapped"

    m = modis[modis.month <= modis_last].copy()
    for metric in METRICS:
        m[metric + "_lo"] = m[metric + "_hi"] = m[metric].astype(float)
    m["source"] = "MODIS"

    cols = ["cell_id", "lon", "lat", "month", "source"] + \
           [c for mt in METRICS for c in (mt, mt + "_lo", mt + "_hi")]
    out = pd.concat([m[cols], v[cols]], ignore_index=True).sort_values(["month", "cell_id"])
    return out.reset_index(drop=True)


# ---------------------------------------------------------------- validation
def backtest(agg, meta, cutoff, metric="fire_days"):
    """Pretend MODIS ended at `cutoff`: fit before it, predict VIIRS after it,
    compare with the MODIS that really exists. Returns (region-month table, summary)."""
    modis, viirs = _prep(agg, meta, "MODIS"), _prep(agg, meta, "VIIRS")
    cutoff = pd.Timestamp(cutoff)
    months = overlap_months(meta, OVERLAP_START, cutoff)
    model = fit_ratio(modis, viirs, metric, months)

    v = viirs[viirs.month > cutoff].copy()
    v["pred"], v["lo"], v["hi"] = predict(model, v, metric)
    pred = v.groupby(["region", "month"])[["pred", "lo", "hi"]].sum()
    obs = modis[modis.month > cutoff].groupby(["region", "month"])[metric].sum().rename("obs")
    t = pd.concat([obs, pred], axis=1).fillna(0).reset_index()

    summary = {
        "n_region_months": len(t),
        "bias_ratio": float(t.pred.sum() / max(t.obs.sum(), 1e-9)),
        "corr_log": float(np.corrcoef(np.log1p(t.obs), np.log1p(t.pred))[0, 1]),
        "mape_pct": float((abs(t.pred - t.obs) / t.obs.clip(lower=1)).median() * 100),
        "coverage_95": float(((t.obs >= t.lo) & (t.obs <= t.hi)).mean()),
    }
    return t, summary
