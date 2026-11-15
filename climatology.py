"""APP CORE (your file): query harmonized data -> series, calendar, anomalies."""
import numpy as np
import pandas as pd


def load_harmonized(path) -> pd.DataFrame:
    return pd.read_parquet(path)


def filter_bbox(df, west, south, east, north):
    return df[df.lon.between(west, east) & df.lat.between(south, north)]


def filter_polygon(df, coords):
    """coords = [(lon, lat), ...] (e.g. from a Leaflet draw event). Ray casting."""
    xs, ys = df.lon.to_numpy(), df.lat.to_numpy()
    inside = np.zeros(len(df), dtype=bool)
    pts = list(coords)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        cond = (y1 > ys) != (y2 > ys)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (x2 - x1) * (ys - y1) / (y2 - y1) + x1
        inside ^= cond & (xs < xint)
    return df[inside]


def monthly_series(df, metric="fire_days", start=None, end=None) -> pd.DataFrame:
    """Region total per month, dense (months with no fire = 0).
    lo/hi sum cell bounds, i.e. assume fully correlated errors (conservative)."""
    cols = [metric, metric + "_lo", metric + "_hi"]
    s = df.groupby("month")[cols].sum()
    s.columns = ["value", "lo", "hi"]
    start = pd.Timestamp(start) if start else s.index.min()
    end = pd.Timestamp(end) if end else s.index.max()
    return s.reindex(pd.date_range(start, end, freq="MS"), fill_value=0.0)


def calendar_matrix(series: pd.DataFrame) -> pd.DataFrame:
    """Rows = year, columns = 1..12. Feed straight into a heatmap."""
    s = series["value"]
    m = s.groupby([s.index.year, s.index.month]).sum().unstack()
    return m.reindex(columns=range(1, 13))


def climatology(series: pd.DataFrame, baseline=(2003, 2022)) -> pd.DataFrame:
    s = series["value"]
    s = s[(s.index.year >= baseline[0]) & (s.index.year <= baseline[1])]
    g = s.groupby(s.index.month)
    return pd.DataFrame({"median": g.median(), "p25": g.quantile(.25),
                         "p75": g.quantile(.75), "mean": g.mean()}).reindex(range(1, 13))


def anomaly_table(series: pd.DataFrame, clim: pd.DataFrame) -> pd.DataFrame:
    t = series.copy()
    t["median"] = t.index.month.map(clim["median"])
    t["ratio_to_median"] = (t["value"] + 1) / (t["median"] + 1)   # defined even when the median is 0
    t["pct_rank"] = t.groupby(t.index.month)["value"].rank(pct=True)
    # significant only if the whole uncertainty band is above/below the climatological IQR
    # the band only reflects calibration uncertainty, so also require a clear change vs the median
    t["flag"] = np.where((t["lo"] > t.index.month.map(clim["p75"])) & (t["ratio_to_median"] >= 1.5), "high",
                 np.where((t["hi"] < t.index.month.map(clim["p25"])) & (t["ratio_to_median"] <= 1 / 1.5), "low", ""))
    return t


def current_vs_history(series: pd.DataFrame, clim: pd.DataFrame, month=None) -> dict:
    month = pd.Timestamp(month) if month else series.index.max()
    v = float(series.loc[month, "value"])
    med = float(clim.loc[month.month, "median"])
    hist = series["value"][(series.index.month == month.month) & (series.index < month)]
    return {"month": month.strftime("%Y-%m"), "value": v, "median": med,
            "ratio": (v + 1) / (med + 1),
            "rank": int((hist < v).sum() + 1), "of": int(len(hist) + 1)}
