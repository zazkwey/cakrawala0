"""Smoke test for the app core (no Streamlit). Works on synthetic or real harmonized output."""
import json

import pandas as pd

from climatology import (anomaly_table, calendar_matrix, climatology, current_vs_history,
                         filter_bbox, load_harmonized, monthly_series)
from config import PROCESSED_DIR

df = load_harmonized(PROCESSED_DIR / "harmonized_monthly.parquet")
df = df[df.month >= "2002-07-01"]                                   # same rules as the app
meta_file = PROCESSED_DIR / "meta.json"
if meta_file.exists():
    df = df[df.month <= pd.Timestamp(json.loads(meta_file.read_text())["last_complete_month"])]

sub = filter_bbox(df, df.lon.quantile(.3), df.lat.quantile(.3), df.lon.quantile(.7), df.lat.quantile(.7))
series = monthly_series(sub, "fire_days", start=df.month.min(), end=df.month.max())
clim = climatology(series, (2003, 2022))
print(calendar_matrix(series).tail(4).round(0))
print(anomaly_table(series, clim).tail(3))
print(current_vs_history(series, clim))
