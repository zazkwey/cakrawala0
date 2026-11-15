"""Burn Activity Calendar (MODIS + VIIRS FIRMS hotspots).   Run:  streamlit run app.py"""
import datetime as dt
import json

import altair as alt
import folium
import numpy as np
import pandas as pd
import shapely
import streamlit as st
from folium.plugins import Draw
from shapely.geometry import Polygon, box, mapping
from shapely.ops import unary_union
from streamlit_folium import st_folium

from climatology import (anomaly_table, calendar_matrix, climatology, current_vs_history,
                         filter_bbox, filter_polygon, load_harmonized, monthly_series)
from config import PROCESSED_DIR
from geo import (DETAIL_DEG, DETAIL_MAX_PIXELS, assign_province, count_overlay, land_geometry,
                 load_cities, load_provinces)

DATA_FILE = PROCESSED_DIR / "harmonized_monthly.parquet"
HOT_DIR = PROCESSED_DIR / "hotspots"
CONT_FILE = PROCESSED_DIR / "continuity_modis.parquet"
META_FILE = PROCESSED_DIR / "meta.json"
HOT_LIMIT = 300_000
HOT_COLS = ["sensor", "satellite", "latitude", "longitude", "acq_date", "acq_time", "confidence", "daynight", "frp"]
MTD_CANDIDATES = [("SNPP", 2013), ("NOAA20", 2019)]
CITY_RADIUS_KM = 25

METRICS = {
    "n_hotspots": dict(label="Hotspots detected", unit="detections",
                       help="Number of satellite hot-pixel detections (MODIS-equivalent). Best validated metric."),
    "fire_days": dict(label="Fire-days", unit="cell-days",
                      help="For every 5 km cell, the number of days with at least one detection, added up. Measures how widely "
                           "and how long fire was present, not how many pixels lit up."),
    "frp_sum": dict(label="Fire intensity (FRP)", unit="MW",
                    help="Fire radiative power added up (megawatts): how much heat is being released. Noisier than the counts."),
}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Green (fewer detections) -> Neutral -> Amber/Red (more detections)
CLASSES = [
    ("Much lower (under 1/2)", "#1b7837"),
    ("Lower (1/2 to 2/3)", "#a6dba0"),
    ("Normal (2/3 to 1.5x)", "#f0f0f0"),
    ("Higher (1.5 to 2x)", "#fdae61"),
    ("Much higher (2 to 4x)", "#e34a33"),
    ("Extreme (over 4x)", "#99000d"),
]

st.set_page_config(page_title="Burn Activity Calendar", layout="wide")


# =============================================================== data + cached helpers
@st.cache_data(show_spinner="Loading harmonized data...")
def load():
    return load_harmonized(DATA_FILE)


if not DATA_FILE.exists():
    st.error(f"No data at {DATA_FILE}. Run `python synthetic.py harmonized` (fake data) "
             "or `python run_pipeline.py --indonesia --demo` (real data) first.")
    st.stop()

meta = json.loads(META_FILE.read_text()) if META_FILE.exists() else {}
df = load()
df = df[df.month >= pd.Timestamp("2002-07-01")]
if "last_complete_month" in meta:
    df = df[df.month <= pd.Timestamp(meta["last_complete_month"])]
lon_min, lon_max, lat_min, lat_max = df.lon.min(), df.lon.max(), df.lat.min(), df.lat.max()
modis_last = pd.Timestamp(meta["modis_last_month"]) if meta.get("modis_last_month") else None


def hot_filter(start, end, geom):
    import pyarrow as pa
    import pyarrow.dataset as pds
    west, south, east, north = geom.bounds
    return ((pds.field("year") >= start.year) & (pds.field("year") <= end.year)
            & (pds.field("acq_date") >= pa.scalar(start, pa.date32())) & (pds.field("acq_date") <= pa.scalar(end, pa.date32()))
            & (pds.field("longitude") >= west) & (pds.field("longitude") <= east)
            & (pds.field("latitude") >= south) & (pds.field("latitude") <= north))


def _prepared(geom_wkt, land_only):
    geom = shapely.from_wkt(geom_wkt)
    shapely.prepare(geom)
    land = land_geometry() if land_only else None
    if land is not None:
        shapely.prepare(land)
    return geom, land


def _inside(geom, land, lon, lat):
    keep = shapely.contains_xy(geom, lon, lat)
    if land is not None:
        keep &= shapely.contains_xy(land, lon, lat)
    return keep


def _scanner(filt, columns, batch=500_000):
    import pyarrow.dataset as pds
    return pds.dataset(HOT_DIR, format="parquet", partitioning="hive").scanner(filter=filt, columns=columns, batch_size=batch)


def collect_hotspots(start, end, geom, land_only):
    """Raw detections inside the SAME area as the statistics, streamed and capped at HOT_LIMIT."""
    shapely.prepare(geom)
    land = land_geometry() if land_only else None
    if land is not None:
        shapely.prepare(land)
    parts, total, truncated = [], 0, False
    for batch in _scanner(hot_filter(start, end, geom), HOT_COLS, 200_000).to_batches():
        b = batch.to_pandas()
        b = b[_inside(geom, land, b.longitude.to_numpy(), b.latitude.to_numpy())]
        parts.append(b)
        total += len(b)
        if total >= HOT_LIMIT:
            truncated = True
            break
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=HOT_COLS)
    return out.head(HOT_LIMIT), truncated


@st.cache_data(show_spinner="Counting raw alerts per ~1 km cell...", max_entries=6)
def detail_counts(start, end, geom_wkt, land_only, months_t, _stamp):
    geom, land = _prepared(geom_wkt, land_only)
    acc = None
    for batch in _scanner(hot_filter(start, end, geom), ["longitude", "latitude", "acq_date"]).to_batches():
        b = batch.to_pandas()
        lon, lat = b.longitude.to_numpy(), b.latitude.to_numpy()
        keep = _inside(geom, land, lon, lat)
        if len(months_t) < 12:
            keep &= pd.to_datetime(b.acq_date).dt.month.isin(months_t).to_numpy()
        if not keep.any():
            continue
        c = pd.DataFrame({"ix": np.floor((lon[keep] + 180) / DETAIL_DEG).astype("int64"),
                          "iy": np.floor((lat[keep] + 90) / DETAIL_DEG).astype("int64")}).value_counts()
        acc = c if acc is None else acc.add(c, fill_value=0)
    return pd.DataFrame(columns=["ix", "iy", "n"]) if acc is None else acc.rename("n").reset_index()


@st.cache_data(show_spinner="Counting alerts...", max_entries=8)
def count_alerts(start, end, geom_wkt, land_only, months_t, _stamp):
    geom, land = _prepared(geom_wkt, land_only)
    tot = {"MODIS": 0, "VIIRS": 0}
    for batch in _scanner(hot_filter(start, end, geom), ["sensor", "longitude", "latitude", "acq_date"]).to_batches():
        b = batch.to_pandas()
        keep = _inside(geom, land, b.longitude.to_numpy(), b.latitude.to_numpy())
        if len(months_t) < 12:
            keep &= pd.to_datetime(b.acq_date).dt.month.isin(months_t).to_numpy()
        for s_, n_ in b.sensor[keep].astype(str).value_counts().items():
            tot[s_] = tot.get(s_, 0) + int(n_)
    return tot


@st.cache_data(show_spinner="Comparing this month with the same days in previous years...", max_entries=6)
def daily_ref_counts(geom_wkt, land_only, month, first_year, last_year, ref_sat, _stamp):
    """Alerts per day (one reference satellite, so years are comparable) for one calendar month of each year."""
    geom, land = _prepared(geom_wkt, land_only)
    rows = []
    for yr in range(first_year, last_year + 1):
        start = dt.date(yr, month, 1)
        end = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date()
        for batch in _scanner(hot_filter(start, end, geom), ["satellite", "longitude", "latitude", "acq_date"]).to_batches():
            b = batch.to_pandas()
            b = b[b.satellite.astype(str) == ref_sat]
            if b.empty:
                continue
            b = b[_inside(geom, land, b.longitude.to_numpy(), b.latitude.to_numpy())]
            rows.append(pd.DataFrame({"year": yr, "day": pd.to_datetime(b.acq_date).dt.day.to_numpy()}))
    if not rows:
        return pd.DataFrame(columns=["year", "day", "n"])
    return pd.concat(rows).groupby(["year", "day"]).size().rename("n").reset_index()


@st.cache_data(show_spinner="Matching cells to provinces (first run only)...")
def cell_province(_stamp):
    c = pd.read_parquet(DATA_FILE, columns=["cell_id", "lon", "lat"]).drop_duplicates("cell_id")
    c["province"] = assign_province(c.lon.to_numpy(), c.lat.to_numpy())
    return c.set_index("cell_id")["province"]


def city_exposure(cells, metric, radius_km=CITY_RADIUS_KM):
    """Activity (sum of `metric` over 5 km cells) within radius_km of each city."""
    if cells.empty:
        return pd.DataFrame(columns=["City", "Population", "Activity"])
    out = []
    clon, clat, val = cells.lon.to_numpy(), cells.lat.to_numpy(), cells[metric].to_numpy()
    for name, pop, lat, lon in load_cities():
        if not (clon.min() - 0.5 <= lon <= clon.max() + 0.5 and clat.min() - 0.5 <= lat <= clat.max() + 0.5):
            continue
        dx = (clon - lon) * 111.32 * np.cos(np.radians(lat))
        dy = (clat - lat) * 110.57
        out.append((name, int(pop), lat, lon, float(val[(dx * dx + dy * dy) <= radius_km ** 2].sum())))
    return pd.DataFrame(out, columns=["City", "Population", "lat", "lon", "Activity"])


def slug(text):
    return "".join(ch if ch.isalnum() else "_" for ch in text).strip("_").lower()[:40] or "area"


def set_date_window(s_date: dt.date, e_date: dt.date):
    st.session_state["start_date"] = s_date
    st.session_state["end_date"] = e_date


# =============================================================== sidebar: 1 where, 2 when, 3 what
PROVS = load_provinces()
cell_prov = cell_province(DATA_FILE.stat().st_mtime)
prov_col = df.cell_id.map(cell_prov)

st.sidebar.title("Burn Activity Calendar")
method = st.sidebar.radio("Choose the area by", ["Province", "Drawing on map", "Coordinates"], key="method",
                          help="Province: pick from a list. Drawing: draw a rectangle or polygon on the map. "
                               "Coordinates: type a bounding box.")
all_months = pd.date_range(df.month.min(), df.month.max(), freq="MS")
y0, y1 = int(all_months.min().year), int(all_months.max().year)
min_date = all_months.min().date()
max_date = (all_months.max() + pd.offsets.MonthEnd(0)).date()
default_start = min_date

if "start_date" not in st.session_state:
    st.session_state["start_date"] = default_start
if "end_date" not in st.session_state:
    st.session_state["end_date"] = max_date

with st.sidebar.expander("Advanced", expanded=False):
    mask_land = st.checkbox("Hide cells over the sea", True, key="mask_land",
                            help="Natural Earth coastlines are simplified, so a few coastal cells fall in the sea, and FIRMS "
                                 "also reports offshore detections (ships, platforms). Hiding them keeps totals on land. "
                                 "Cost: about 1-3% of activity near coasts is dropped. Only applies to drawn areas and boxes; "
                                 "provinces are always land.")
    _base_end = min(modis_last.year if modis_last is not None else y1 - 1, y1 - 1)
    baseline = st.slider("Years that define 'normal'", y0, y1, (y0 + 1, max(y0 + 2, _base_end)), key="baseline",
                         help="'Normal' for a month is the median of that calendar month over these years. The default stops at "
                              f"{_base_end} because later years in this demo are built from VIIRS, which is biased low in "
                              "intense seasons. When MODIS is still running, extend it to the latest year.")

if method == "Province":
    names = st.sidebar.multiselect("Provinces (empty = all Indonesia)", sorted(PROVS), key="areas")
    if names:
        sub, region_geom = df[prov_col.isin(names)], unary_union([PROVS[n] for n in names])
    else:
        sub, region_geom = df[prov_col.notna()], land_geometry()
    polygon = None
    area_name = ", ".join(names) if 0 < len(names) <= 3 else (f"{len(names)} provinces" if names else "Indonesia")
elif method == "Coordinates":
    names, polygon = [], None
    c1, c2 = st.sidebar.columns(2)
    w = c1.number_input("West", value=float(lon_min), format="%.3f")
    e = c2.number_input("East", value=float(lon_max), format="%.3f")
    s_ = c1.number_input("South", value=float(lat_min), format="%.3f")
    n = c2.number_input("North", value=float(lat_max), format="%.3f")
    region_geom, sub, area_name = box(w, s_, e, n), filter_bbox(df, w, s_, e, n), "Selected box"
else:
    names = []
    st.sidebar.caption("Draw a rectangle or polygon on the map. Use 'Clear drawn area' to start again "
                       "(the map's trash tool does not clear it).")
    if st.sidebar.button("Clear drawn area"):
        st.session_state.pop("drawn_polygon", None)
        st.session_state["map_epoch"] = st.session_state.get("map_epoch", 0) + 1
    polygon = st.session_state.get("drawn_polygon")
    region_geom = Polygon(polygon) if polygon else box(lon_min, lat_min, lon_max, lat_max)
    sub = filter_polygon(df, polygon) if polygon else df
    area_name = "Drawn area" if polygon else "All of Indonesia (nothing drawn yet)"

hidden_note = ""
if mask_land and method != "Province":
    before = sub.n_hotspots.sum()
    sub = sub[prov_col.reindex(sub.index).notna()]
    hidden_note = f"Sea-hiding removed {1 - sub.n_hotspots.sum() / max(before, 1e-9):.1%} of activity here."
if sub.empty:
    if method == "Drawing on map" and polygon:
        st.warning("No land fire activity inside the drawn area, so everything is shown. Draw again or clear the area.")
        polygon, region_geom, area_name = None, box(lon_min, lat_min, lon_max, lat_max), "All of Indonesia (nothing drawn yet)"
        sub = df[prov_col.notna()] if mask_land else df
    else:
        st.warning("No fire activity recorded in this area. Try a larger area.")
        st.stop()
outline_geom = region_geom if (names or polygon or method == "Coordinates") else None

# DD/MM/YYYY date pickers instead of cramped multi-decade slider
st.sidebar.markdown("**Period to show (DD/MM/YYYY)**")
dc1, dc2 = st.sidebar.columns(2)
start_date = dc1.date_input("From", min_value=min_date, max_value=max_date, format="DD/MM/YYYY", key="start_date",
                            help="Applies to the calendar, trend, map and downloads.")
end_date = dc2.date_input("To", min_value=min_date, max_value=max_date, format="DD/MM/YYYY", key="end_date",
                          help="Applies to the calendar, trend, map and downloads.")
if start_date > end_date:
    st.sidebar.warning("'From' date is after 'To' date; swapping automatically.")
    start_date, end_date = end_date, start_date

if start_date != min_date or end_date != max_date:
    st.sidebar.button("Reset to full period", on_click=set_date_window, args=(min_date, max_date),
                      use_container_width=True)

d0 = pd.Timestamp(start_date).replace(day=1)
d1 = pd.Timestamp(end_date).replace(day=1)
end_day = end_date
r0, r1 = d0.strftime("%Y-%m"), d1.strftime("%Y-%m")

metric = st.sidebar.selectbox("Measure", list(METRICS), format_func=lambda k: METRICS[k]["label"], key="metric",
                              help="\n\n".join(f"**{v['label']}**: {v['help']}" for v in METRICS.values()))
U = METRICS[metric]["unit"]
ML = METRICS[metric]["label"]

# =============================================================== analytics shared by the tabs
full = monthly_series(sub, metric, start=all_months.min(), end=all_months.max())
clim = climatology(full, baseline)
anom = anomaly_table(full, clim)
shown = full[(full.index >= d0) & (full.index <= d1)]
latest = current_vs_history(full, clim)
months_of_year = list(range(1, 13))

# Right-now (current partial month, like-for-like with previous years)
mtd = None
_ends = {s: pd.Timestamp(meta["data_end_by_satellite"][f"VIIRS/{s}"]) for s, _ in MTD_CANDIDATES
         if f"VIIRS/{s}" in meta.get("data_end_by_satellite", {})}
if HOT_DIR.exists() and _ends and "last_complete_month" in meta:
    _newest = max(_ends.values())
    ref_sat, ref_first = next(((s, fy) for s, fy in MTD_CANDIDATES if s in _ends and _ends[s] >= _newest - pd.Timedelta(days=3)),
                              (None, None))
    ref_end = _ends.get(ref_sat)
    if ref_sat and ref_end > pd.Timestamp(meta["last_complete_month"]) + pd.offsets.MonthEnd(0):
        daily = daily_ref_counts(region_geom.wkt, method != "Province" and mask_land, ref_end.month,
                                 max(ref_first, y0), ref_end.year, ref_sat, HOT_DIR.stat().st_mtime)
        D = ref_end.day
        past = daily[daily.year < ref_end.year]
        cur = daily[daily.year == ref_end.year]
        years = sorted(past.year.unique())
        if years:
            tot_past = past[past.day <= D].groupby("year").n.sum().reindex(years, fill_value=0)
            cur_tot = int(cur[cur.day <= D].n.sum())
            mtd = dict(ref_end=ref_end, D=D, cur_total=cur_tot, typical=float(tot_past.median()), ref_sat=ref_sat,
                       ratio=(cur_tot + 1) / (tot_past.median() + 1), n_years=len(years) + 1,
                       rank_desc=int(len(years) + 1 - ((tot_past < cur_tot).sum() + 1) + 1), daily=daily, years=years,
                       first=min(years))


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# =============================================================== header
st.title("Burn Activity Calendar")
st.markdown(f"How much fire is in {area_name} compared with what is normal for it and the time of year, "
            "using NASA FIRMS satellite hotspots (MODIS history joined to VIIRS).")
parts = [f"**{latest['month']}** was **{latest['ratio']:.1f}x** a normal month ({ML.lower()}; "
         f"{ordinal(latest['of'] - latest['rank'] + 1)} highest of {latest['of']} such months)"]
if mtd:
    parts.append(f"so far in {mtd['ref_end']:%B}: **{mtd['ratio']:.1f}x** typical for the same days")
st.info(" · ".join(parts))
chips = [
    f"Showing {start_date:%d/%m/%Y} – {end_date:%d/%m/%Y}",
    f"Data through {pd.Timestamp(meta['last_complete_month']):%b %Y} (complete months)" if "last_complete_month" in meta else "Synthetic data",
]
if modis_last is not None:
    chips.append(f"DEMO: MODIS retirement simulated after {modis_last:%b %Y}")
elif meta and not meta.get("harmonization_active", True):
    chips.append("MODIS only (no VIIRS mapping)")
st.caption(" | ".join(chips))

with st.expander("New here? How to use this page (30 seconds)"):
    st.markdown(
        "1. Pick an area in the sidebar (province, drawing on the map, or coordinates).\n"
        "2. Pick the date range (`DD/MM/YYYY`) and what to measure.\n"
        "3. Read the map for where the burning is, then the tabs below:\n"
        "   - *Right now*: is this month, so far, worse than usual?\n"
        "   - *History*: calendar and trend, with one-click buttons on months that stood out.\n"
        "   - *Does it work?*: how well VIIRS reproduces the MODIS record.\n"
        "   - *Download*: files for reports and field teams.")

# =============================================================== map (draw the area here)
st.subheader("Map")
if hidden_note:
    st.caption(f"Note: {hidden_note}")
if method == "Drawing on map":
    st.info("Use the rectangle or polygon tool at the left edge of the map. The whole page then updates for that area."
            if not polygon else "Showing your drawn area. Draw another shape to replace it, or press 'Clear drawn area' in the sidebar.")
mc1, mc2 = st.columns([2, 3])
map_style = mc1.radio("Map style", ["Heatmap (smooth)", "Raw alerts (1 km)"], key="map_style", horizontal=True,
                      help="Heatmap: harmonized activity, smoothed. Matches the calendar and works for all of Indonesia. "
                           "Raw alerts: every satellite detection per ~1 km cell, for exact locations; needs an area "
                           "smaller than about 20 x 20 degrees.")
layers = mc2.multiselect("Extra layers", ["Cities (colour = burning within 25 km)", "Province outlines"],
                         default=["Cities (colour = burning within 25 km)"], key="layers")
with st.expander("Only show some months of the year (for example the dry season)"):
    months_sel = st.multiselect("Months of the year", MONTHS, default=MONTHS, key="moy")
mnums = [MONTHS.index(m) + 1 for m in months_sel] or months_of_year
period = sub[(sub.month >= d0) & (sub.month <= d1) & sub.month.dt.month.isin(mnums)]
cells = period.groupby(["cell_id", "lon", "lat"], as_index=False)[metric].sum()
cells = cells[cells[metric] > 0]

hm = folium.Map(location=[-2.5, 118.0], zoom_start=5, tiles=None, zoom_control=True)
folium.TileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}",
                 attr="Esri World Shaded Relief", name="Shaded relief").add_to(hm)
folium.TileLayer("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/BlueMarble_ShadedRelief_Bathymetry/default/"
                 "GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg", attr="NASA EOSDIS GIBS", name="NASA Blue Marble",
                 show=False, max_zoom=8).add_to(hm)
folium.TileLayer("OpenStreetMap", name="OpenStreetMap", show=False).add_to(hm)
low = high = None
fit = None
kind = "harmonized"
if not cells.empty:
    fit = [[cells.lat.min(), cells.lon.min()], [cells.lat.max(), cells.lon.max()]]
    use_raw = map_style.startswith("Raw")
    if use_raw:
        wst, sth, est, nth = region_geom.bounds
        if not HOT_DIR.exists():
            st.info("Raw alerts need the hotspot files (run run_pipeline.py on real FIRMS data). Showing the heatmap instead.")
            use_raw = False
        elif ((est - wst) / DETAIL_DEG) * ((nth - sth) / DETAIL_DEG) > DETAIL_MAX_PIXELS:
            st.info("Raw alerts work for areas up to about 20 x 20 degrees. Pick a province or draw a smaller area. "
                    "Showing the heatmap instead.")
            use_raw = False
    if use_raw:
        counts = detail_counts(start_date, end_day, region_geom.wkt, method != "Province" and mask_land, tuple(mnums),
                               HOT_DIR.stat().st_mtime)
        if len(counts):
            uri, bounds, low, high = count_overlay(counts)
            folium.raster_layers.ImageOverlay(uri, bounds=bounds, opacity=1.0, name="Raw alerts (~1 km)").add_to(hm)
            fit, kind = bounds, "raw"
            hm.get_root().script.add_child(folium.Element(f"{hm.get_name()}.attributionControl.setPrefix(false);"))
            hm.get_root().html.add_child(folium.Element(
                "<style>.leaflet-control-attribution{font-size:8px !important;line-height:1.1 !important;opacity:0.7;padding:1px 4px !important;}</style>"
))
        else:
            use_raw = False
    if not use_raw:
        from folium.plugins import HeatMap
        w_ = np.log1p(cells[metric])
        w_ = (w_ / w_.quantile(0.98)).clip(0, 1)
        extent = max(np.ptp(cells.lon), np.ptp(cells.lat), 0.5)
        top = cells.assign(weight=w_).nlargest(150_000, "weight")
        HeatMap(top[["lat", "lon", "weight"]].values.tolist(), radius=int(np.clip(14 - np.log2(extent), 5, 16)), blur=10,
                min_opacity=0.25, max_zoom=9,
                gradient={0.2: "#ffffb2", 0.4: "#fecc5c", 0.6: "#fd8d3c", 0.8: "#f03b20", 1.0: "#bd0026"},
                name="Fire heatmap").add_to(hm)
else:
    st.info("No activity for this selection.")

expo = city_exposure(cells, metric) if not cells.empty else pd.DataFrame()
if layers and layers[0].startswith("Cities") and len(expo):
    mx = max(expo.Activity.max(), 1e-9)
    for r in expo.itertuples():
        hot_c = r.Activity > 0
        folium.CircleMarker([r.lat, r.lon], radius=4 + 5 * (r.Activity / mx) ** 0.5, color="#222", weight=1, fill=True,
                            fill_color="#bd0026" if r.Activity > 0.1 * mx else ("#fd8d3c" if hot_c else "#ffffff"),
                            fill_opacity=0.9, tooltip=f"{r.City} (pop. {r.Population:,}): {r.Activity:,.0f} {U} within "
                                                       f"{CITY_RADIUS_KM} km").add_to(hm)
if "Province outlines" in layers:
    folium.GeoJson({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": mapping(g)} for g in PROVS.values()]}, name="Province outlines",
        style_function=lambda _: {"fillOpacity": 0, "color": "#555555", "weight": 0.6}).add_to(hm)
if outline_geom is not None:
    folium.GeoJson(mapping(outline_geom), name="Selected area",
                   style_function=lambda _: {"fillOpacity": 0, "color": "#111111", "weight": 1.6}).add_to(hm)
if method == "Drawing on map":
    Draw(export=False, draw_options={"polyline": False, "circle": False, "marker": False, "circlemarker": False}).add_to(hm)
if fit:
    hm.fit_bounds(fit)
folium.LayerControl().add_to(hm)
hm.get_root().script.add_child(folium.Element(f"{hm.get_name()}.attributionControl.setPrefix(false);"))
out = st_folium(hm, height=540, use_container_width=True,
                returned_objects=["last_active_drawing"] if method == "Drawing on map" else [],
                key=f"map-{method}-{map_style}-{st.session_state.get('map_epoch', 0)}")
if method == "Drawing on map":
    shp = (out or {}).get("last_active_drawing")
    if shp and shp["geometry"]["type"] == "Polygon":
        new_poly = [tuple(round(c, 6) for c in p) for p in shp["geometry"]["coordinates"][0][:-1]]
        if new_poly != st.session_state.get("drawn_polygon"):
            st.session_state["drawn_polygon"] = new_poly
            st.rerun()

st.markdown('<div style="display:flex;align-items:center;gap:8px;font-size:0.85rem"><span>fewer</span>'
            '<div style="flex:0 0 220px;height:10px;border-radius:3px;background:linear-gradient(90deg,#ffffb2,#fecc5c,#fd8d3c,#f03b20,#bd0026)"></div>'
            '<span>more</span></div>', unsafe_allow_html=True)
what = ("Raw alerts per ~1 km cell, counted as each satellite reported them (not harmonized, so not comparable with the "
        "calendar numbers)" if kind == "raw" else f"{ML} per ~5 km cell, harmonized to MODIS-equivalent (matches the calendar)")
st.caption(f"{what}. Colour scale is logarithmic" + (f" ({low:,.0f} to {high:,.0f}+ per cell)" if high else "") +
           f". Dots are cities over 100,000 people; the bigger and redder, the more burning within {CITY_RADIUS_KM} km. "
           "An alert is a hot pixel at overpass time, not a burned area.")
if HOT_DIR.exists():
    al = count_alerts(start_date, end_day, region_geom.wkt, method != "Province" and mask_land, tuple(mnums), HOT_DIR.stat().st_mtime)
    st.caption(f"Raw alerts in this area and period: {sum(al.values()):,} (MODIS {al.get('MODIS', 0):,}, VIIRS {al.get('VIIRS', 0):,}). "
               "One fire can be detected several times, so this is a count of detections, not of fires.")
with st.expander(f"Which cities are closest to the burning? (within {CITY_RADIUS_KM} km)"):
    if len(expo) and expo.Activity.max() > 0:
        t = expo[expo.Activity > 0].sort_values("Activity", ascending=False).head(10)
        st.dataframe(t.rename(columns={"Activity": f"{ML} ({U})"})[["City", "Population", f"{ML} ({U})"]].round(0),
                     hide_index=True, width="stretch")
        st.caption("Use: prioritise smoke and haze warnings and field checks for the places with people nearest to the fires.")
    else:
        st.write("No city over 100,000 people has burning within this distance in the selected period.")

# =============================================================== tabs
tab_now, tab_hist, tab_val, tab_dl, tab_info = st.tabs(["Right now", "History", "Harmonization", "Download", "About the data"])

# ---------------------------------------------------------------- RIGHT NOW
with tab_now:
    st.markdown("Compares alerts from one satellite "
                f"({mtd['ref_sat'] if mtd else 'VIIRS'}, {mtd['first'] if mtd else ''} onward) over the same days of the month in "
                "previous years.")
    if mtd:
        k1, k2, k3, k4 = st.columns(4)
        k1.metric(f"Alerts, 1-{mtd['D']} {mtd['ref_end']:%b %Y}", f"{mtd['cur_total']:,}")
        k2.metric("Typical for the same days", f"{mtd['typical']:,.0f}", help=f"Median over previous years ({mtd['first']} onward).")
        k3.metric("Compared to typical", f"{mtd['ratio']:.2f}x")
        k4.metric("Rank among years", f"#{mtd['rank_desc']} of {mtd['n_years']}", help="#1 = the most active year for these days.")
        d = mtd["daily"]
        days = range(1, int(pd.Timestamp(mtd["ref_end"]).days_in_month) + 1)
        piv = d.pivot(index="year", columns="day", values="n").reindex(columns=days).fillna(0)
        cum = piv.cumsum(axis=1)
        past_c = cum.loc[mtd["years"]]
        rows = [{"day": dd, "series": "Typical range (middle 50% of years)", "lo": past_c[dd].quantile(.25), "hi": past_c[dd].quantile(.75)}
                for dd in days]
        band = pd.DataFrame(rows)
        med = pd.DataFrame({"day": list(days), "value": [past_c[dd].median() for dd in days], "series": "Typical (median)"})
        now = pd.DataFrame({"day": list(range(1, mtd["D"] + 1)),
                            "value": [cum.loc[mtd["ref_end"].year, dd] if mtd["ref_end"].year in cum.index else 0 for dd in range(1, mtd["D"] + 1)],
                            "series": f"{mtd['ref_end']:%B %Y} so far"})
        dom = ["Typical range (middle 50% of years)", "Typical (median)", f"{mtd['ref_end']:%B %Y} so far"]
        # labelLimit=400 prevents "Typical range (middle 50% of years)" from truncating with "..."
        col = alt.Color("series:N", scale=alt.Scale(domain=dom, range=["#c6dbef", "#6b6b6b", "#bd0026"]),
                        legend=alt.Legend(title=None, orient="top", labelLimit=400))
        st.markdown(f"Running total of alerts through {mtd['ref_end']:%B}. To deduce whether the month is worse than usual, compare the red line with the grey and blue bands.")
        ch = (alt.Chart(band).mark_area(opacity=0.6).encode(x=alt.X("day:Q", title="Day of the month"),
                                                           y=alt.Y("lo:Q", title="Alerts so far this month"), y2="hi:Q", color=col)
              + alt.Chart(med).mark_line(strokeDash=[5, 3]).encode(x="day:Q", y="value:Q", color=col)
              + alt.Chart(now).mark_line(strokeWidth=3).encode(x="day:Q", y="value:Q", color=col))
        st.altair_chart(ch.properties(height=300), width="stretch")
        st.caption(f"Latest data: {mtd['ref_end']:%d/%m/%Y} ({mtd['ref_sat']}, chosen because it is the most current VIIRS satellite "
                   "with a long record). The last day or two can be incomplete because NRT data arrives with a delay. "
                   "Raw alerts, not harmonized.")
        if HOT_DIR.exists() and st.button("Prepare the last 7 days of hotspots for this area", key="btn_week"):
            wk, trunc = collect_hotspots((mtd["ref_end"] - pd.Timedelta(days=6)).date(), mtd["ref_end"].date(), region_geom,
                                         method != "Province" and mask_land)
            st.download_button(f"Download {len(wk):,} hotspots from the last 7 days (CSV)", wk.to_csv(index=False),
                               f"hotspots_last7days_{slug(area_name)}.csv", "text/csv", key="dl_week")
    else:
        st.info("No partial month yet: the data is complete up to the latest day available. When new days arrive "
                "(re-run the pipeline with fresh NRT files) this tab compares them with previous years.")

# ---------------------------------------------------------------- HISTORY
with tab_hist:
    if len(shown) < 12:
        hc1, hc2 = st.columns([3.5, 1])
        hc1.info(
            f"Only {len(shown)} month(s) selected ({start_date:%d/%m/%Y} – {end_date:%d/%m/%Y}). "
            "You need to expand the time period to be able to analyze multi-year seasonal patterns and long-term trends."
        )
        hc2.button("Expand to full period", on_click=set_date_window, args=(min_date, max_date), use_container_width=True)

    k1, k2, k3, k4 = st.columns([1.2, 1, 1, 1])
    # Keep unit in the label so the metric number never truncates to "12,345 detectio..."
    k1, k2, k3, k4 = st.columns([1.2, 1, 1, 1])
    k1.metric(f"Latest complete month ({latest['month']}) · {U}", f"{latest['value']:,.0f}")
    k2.metric(f"Normal for {latest['month'][:3]} ({U})", f"{latest['median']:,.0f}",
              help="Median of the same calendar month over the 'normal' years.")
    k3.metric("Compared to normal", f"{latest['ratio']:.2f}x")
    k4.metric("Rank among same months", f"#{latest['of'] - latest['rank'] + 1} of {latest['of']}",
              help="#1 = the most active such month on record.")

    st.subheader("Calendar")
    cal_mode = st.radio("Show", ["Compared with normal", "Amount"], horizontal=True, key="cal_mode",
                        help="Compared with normal: each month divided by the normal for that calendar month. Amount: the totals.")
    mat = calendar_matrix(shown)
    if cal_mode == "Amount":
        st.markdown(f"**How to read:** each row is a year, each column a month. A square is the total {ML.lower()} for the whole "
                    f"area that month ({U}). ")
        vals, text = mat, mat.map(lambda v: f"{v:,.0f}")
        color = alt.Color(
            "value:Q",
            scale=alt.Scale(
                range=["#e5f5e0", "#a1d99b", "#fee08b", "#fc8d59", "#d73027", "#7f0000"],
                type="sqrt",
            ),
            title=f"{U} per month",
        )
    else:
        st.markdown(f"**How to read:** each square is a month compared with the normal for that calendar month (median of "
                    f"{baseline[0]}-{baseline[1]}). "
                    "The number in a square is how many times normal.")
        ratio = (mat + 1).div(clim["median"] + 1, axis=1)
        cls = ratio.apply(lambda col_: pd.cut(col_, [0, .5, 2 / 3, 1.5, 2, 4, np.inf], right=False, labels=[c[0] for c in CLASSES]).astype(object))
        vals = cls
        text = ratio.map(lambda r: "" if pd.isna(r) or 2 / 3 <= r < 1.5 else f"{r:.1f}x")
        color = alt.Color("value:N", scale=alt.Scale(domain=[c[0] for c in CLASSES], range=[c[1] for c in CLASSES]),
                          legend=alt.Legend(title="Compared with normal", orient="right", labelLimit=300))
    cal = vals.stack(future_stack=True).rename("value").reset_index()
    cal.columns = ["year", "month", "value"]
    cal["label"] = [text.loc[y, m] for y, m in zip(cal.year, cal.month)]
    cal["month_name"] = cal["month"].map(lambda x: MONTHS[int(x) - 1])
    cal["source"] = [("VIIRS-mapped" if (modis_last is not None and pd.Timestamp(year=int(y), month=int(m), day=1) > modis_last) else "MODIS")
                     for y, m in zip(cal.year, cal.month)]
    cal["amount"] = [f"{mat.loc[y, m]:,.0f}" if not pd.isna(mat.loc[y, m]) else "" for y, m in zip(cal.year, cal.month)]
    base_c = alt.Chart(cal).encode(x=alt.X("month_name:O", sort=MONTHS, title=None, axis=alt.Axis(orient="top")),
                                   y=alt.Y("year:O", title=None))
    heat = base_c.mark_rect().encode(color=color, tooltip=["year:O", "month_name:O", alt.Tooltip("amount:N", title=U),
                                                          alt.Tooltip("source:N", title="data from")])
    dark = [CLASSES[0][0], CLASSES[4][0], CLASSES[5][0]]
    txt_color = alt.condition(alt.FieldOneOfPredicate(field="value", oneOf=dark), alt.value("#ffffff"), alt.value("#111111"))
    layer = heat + base_c.mark_text(fontSize=9).encode(text="label:N", color=txt_color) if cal_mode != "Amount" else heat
    st.altair_chart(layer.properties(height=max(300, 19 * cal.year.nunique())), width="stretch")
    if modis_last is not None:
        st.caption(f"Months after {modis_last:%b %Y} come from VIIRS mapped to MODIS-equivalent (hover a square to see its source).")

    with st.expander("View normal baseline & comparison table (Jan–Dec)"):
        sel_by_moy = shown.groupby(shown.index.month)["value"].mean()
        base_tbl = pd.DataFrame({
            "Month": MONTHS,
            f"Normal ({U})": [clim.loc[m, "median"] for m in range(1, 13)],
            "Typical 25th–75th pct": [f"{clim.loc[m, 'p25']:,.0f} – {clim.loc[m, 'p75']:,.0f}" for m in range(1, 13)],
            f"Selected avg ({U})": [sel_by_moy.get(m, np.nan) for m in range(1, 13)],
            "Selected vs normal": [(sel_by_moy.get(m, np.nan) + 1) / (clim.loc[m, "median"] + 1) for m in range(1, 13)],
        })
        st.dataframe(
            base_tbl,
            hide_index=True,
            width="stretch",
            column_config={
                f"Normal ({U})": st.column_config.NumberColumn(format="%,.0f"),
                f"Selected avg ({U})": st.column_config.NumberColumn(format="%,.0f"),
                "Selected vs normal": st.column_config.NumberColumn(format="%.2fx"),
            },
        )

    st.subheader("Trend against normal")
    show_real = False
    if CONT_FILE.exists() and modis_last is not None:
        show_real = st.checkbox(f"Also show the real MODIS after {modis_last:%b %Y} (hidden from the model) to check the mapping",
                                True, key="show_real")
    st.markdown(f"**How to read:** blue is the monthly {ML.lower()} for the area; grey is the normal for that calendar month. "
                + (f"After {modis_last:%b %Y} the blue line is VIIRS mapped to MODIS-equivalent, with its 95% band; the black line is "
                   "what MODIS really recorded. Close lines mean the mapping works." if modis_last is not None else ""))
    ts = shown.reset_index().rename(columns={"index": "month"})
    ts["normal"] = ts["month"].dt.month.map(clim["median"])
    N_ACT, N_NORM, N_REAL, N_BAND = "Harmonized activity", "Normal for that month", "Real MODIS (hidden)", "95% band (VIIRS-mapped)"
    dom = [N_ACT, N_NORM, N_REAL, N_BAND]
    col = alt.Color("series:N", scale=alt.Scale(domain=dom, range=["#1f77b4", "#8c8c8c", "#000000", "#9ecae1"]),
                    legend=alt.Legend(title=None, orient="top", symbolType="stroke", symbolStrokeWidth=3, labelLimit=400))
    layers_c = [alt.Chart(ts.assign(series=N_ACT)).mark_line().encode(x=alt.X("month:T", title=None), y=alt.Y("value:Q", title=f"{ML} ({U})"), color=col),
                alt.Chart(ts.assign(series=N_NORM)).mark_line(strokeDash=[5, 3]).encode(x="month:T", y="normal:Q", color=col)]
    if modis_last is not None:
        mp = ts[ts.month > modis_last]
        if len(mp):
            layers_c.append(alt.Chart(mp.assign(series=N_BAND)).mark_area(opacity=0.45).encode(x="month:T", y="lo:Q", y2="hi:Q", color=col))
        rule = pd.DataFrame({"month": [modis_last], "t": ["MODIS retired here (simulated)"]})
        layers_c.append(alt.Chart(rule).mark_rule(color="#999999", strokeDash=[2, 2]).encode(x="month:T"))
        layers_c.append(alt.Chart(rule).mark_text(align="left", dx=4, dy=-4, baseline="bottom", color="#666666").encode(x="month:T", y=alt.value(8), text="t:N"))
        if show_real:
            real_all = pd.read_parquet(CONT_FILE)
            cell_ = real_all.drop_duplicates("cell_id")
            ins = shapely.contains_xy(region_geom, cell_.lon.to_numpy(), cell_.lat.to_numpy())
            if method != "Province" and mask_land:
                ins &= pd.notna(assign_province(cell_.lon.to_numpy(), cell_.lat.to_numpy()))
            real_a = real_all[real_all.cell_id.isin(set(cell_.cell_id[ins]))]
            ra = real_a.groupby("month")[metric].sum().reindex(ts[ts.month > modis_last].month, fill_value=0.0).rename("real").reset_index()
            layers_c.append(alt.Chart(ra.assign(series=N_REAL)).mark_line(strokeWidth=5, opacity=0.55).encode(x="month:T", y="real:Q", color=col))
            layers_c.append(alt.Chart(mp.assign(series=N_ACT)).mark_line(strokeWidth=1.5).encode(x="month:T", y="value:Q", color=col))
    st.altair_chart(alt.layer(*layers_c).properties(height=320), width="stretch")

    st.subheader("Months that stood out")
    st.markdown("**What it is:** the months in your selection that were clearly above or below normal. **How to use it:** pick any "
                "standout episode below and click Focus map & calendar to zoom the whole dashboard to that `DD/MM/YYYY` window.")
    pick = st.radio("Show", ["High", "Low", "Both"], horizontal=True, key="flag_pick")
    u = anom[(anom.index >= d0) & (anom.index <= d1) & (anom.flag != "")]
    u = u[u.flag.str.lower() == pick.lower()] if pick != "Both" else u
    u = u.sort_values("ratio_to_median", ascending=(pick == "Low"))

    if not u.empty:
        # One-click DD/MM/YYYY jump control for standout months
        jump_options = list(u.index)
        jc1, jc2, jc3 = st.columns([2.2, 1.4, 1.1])
        chosen_ts = jc1.selectbox(
            "Jump to standout month (DD/MM/YYYY)",
            options=jump_options,
            format_func=lambda ts_m: (
                f"{ts_m:%d/%m/%Y} – {(ts_m + pd.offsets.MonthEnd(0)):%d/%m/%Y}  "
                f"({ts_m:%b %Y} · {u.loc[ts_m, 'ratio_to_median']:.2f}x normal)"
            ),
            key="standout_jump_select",
            label_visibility="collapsed",
        )
        m_start = chosen_ts.date()
        m_end = (chosen_ts + pd.offsets.MonthEnd(0)).date()
        jc2.button(
            f"Focus {m_start:%d/%m/%Y} – {m_end:%d/%m/%Y}",
            on_click=set_date_window,
            args=(m_start, m_end),
            use_container_width=True,
            type="primary",
        )
        jc3.button(
            "Show all dates",
            on_click=set_date_window,
            args=(min_date, max_date),
            use_container_width=True,
            disabled=(start_date == min_date and end_date == max_date),
        )

        def _severity_label(row):
            r = row["ratio_to_median"]
            if row["flag"].lower() == "low":
                return "Much lower (<0.5x)" if r < 0.5 else "Lower (0.5–0.67x)"
            if r > 4.0:
                return "Extreme (>4x)"
            if r > 2.0:
                return "Much higher (2–4x)"
            return "Higher (1.5–2x)"

        tbl = pd.DataFrame({
            "Month": u.index.strftime("%b %Y"),
            "Period (DD/MM/YYYY)": [f"{ts_m:%d/%m/%Y} – {(ts_m + pd.offsets.MonthEnd(0)):%d/%m/%Y}" for ts_m in u.index],
            f"Detected ({U})": u["value"].round(0),
            f"Normal ({U})": u["median"].round(0),
            f"Diff vs normal": (u["value"] - u["median"]).round(0),
            "Times normal": u["ratio_to_median"].round(2),
            "95% Band": [f"{lo:,.0f} – {hi:,.0f}" for lo, hi in zip(u["lo"], u["hi"])],
            "Status": [_severity_label(row) for _, row in u.iterrows()],
        })
        st.dataframe(
            tbl,
            hide_index=True,
            width="stretch",
            column_config={
                f"Detected ({U})": st.column_config.NumberColumn(format="%,.0f"),
                f"Normal ({U})": st.column_config.NumberColumn(format="%,.0f"),
                "Diff vs normal": st.column_config.NumberColumn(format="%+,.0f"),
                "Times normal": st.column_config.NumberColumn(format="%.2fx"),
            },
        )
    else:
        st.info("No months stood out as unusually high or low in this date range.")
        if start_date != min_date or end_date != max_date:
            st.button("Reset to full period", on_click=set_date_window, args=(min_date, max_date))

# ---------------------------------------------------------------- VALIDATION
with tab_val:
    st.markdown("MODIS is nearing the end of its life, so this app maps VIIRS onto MODIS-equivalent "
                "values. To test accuracy honestly, the model is not shown MODIS after the cutoff (a simulated retirement), "
                "and its VIIRS-based estimate is compared against the real MODIS data that was held out.")

    bt = meta.get("backtest") or {}
    has_area_val = False
    cc = pd.DataFrame()
    fr = fm = pd.Series(dtype=bool)
    after = pd.DatetimeIndex([])

    if CONT_FILE.exists() and modis_last is not None:
        real_all = pd.read_parquet(CONT_FILE)
        cell_ = real_all.drop_duplicates("cell_id")
        ins = shapely.contains_xy(region_geom, cell_.lon.to_numpy(), cell_.lat.to_numpy())
        if method != "Province" and mask_land:
            ins &= pd.notna(assign_province(cell_.lon.to_numpy(), cell_.lat.to_numpy()))
        real_a = real_all[real_all.cell_id.isin(set(cell_.cell_id[ins]))]
        after = pd.date_range(modis_last + pd.offsets.MonthBegin(1), full.index.max(), freq="MS")
        rs = real_a.groupby("month")[metric].sum().reindex(after, fill_value=0.0)
        mp = full.reindex(after)
        if len(after) and rs.sum() > 0:
            has_area_val = True
            cc = pd.DataFrame({
                "month": after,
                "real": rs.to_numpy(),
                "mapped": mp["value"].to_numpy(),
                "lo": mp["lo"].to_numpy(),
                "hi": mp["hi"].to_numpy(),
            })
            med_ = cc.month.dt.month.map(clim["median"])
            p75 = cc.month.dt.month.map(clim["p75"])
            flag = lambda v: (v > p75) & ((v + 1) / (med_ + 1) >= 1.5)
            fr, fm = flag(cc.real), flag(cc.mapped)

            vol_ratio = cc.mapped.sum() / max(cc.real.sum(), 1e-9)
            agree_pct = (fr == fm).mean()
            verdict_msg = (
                f"Verdict for {area_name} ({after[0]:%d/%m/%Y} – {(after[-1] + pd.offsets.MonthEnd(0)):%d/%m/%Y}): "
                f"VIIRS-mapped totals captured {vol_ratio:.0%} of real MODIS volume and agreed with MODIS on "
                f"high-fire vs. normal alerts in {agree_pct:.0%} of months ({int((fr == fm).sum())} of {len(cc)})."
            )
            if 0.8 <= vol_ratio <= 1.25 and agree_pct >= 0.8:
                st.success(verdict_msg)
            else:
                st.warning(verdict_msg)

    v_tab1, v_tab2, v_tab3 = st.tabs([
        f"1. Selected Area ({area_name})",
        "2. All-Indonesia Score Card",
        "3. How Validation Works",
    ])

    with v_tab1:
        if has_area_val:
            a, b, c = st.columns(3)
            a.metric("Mapped total vs real MODIS", f"{cc.mapped.sum() / max(cc.real.sum(), 1e-9):.0%}",
                     help="100% = identical total volume across the validation period (all months).")
            b.metric("Months MODIS flags as high", f"{int(fr.sum())} of {len(cc)}")
            c.metric("Also flagged from VIIRS alone",
                     f"{int((fr & fm).sum())} of {int(fr.sum())}" if fr.sum() else "n/a",
                     help="Would responders receive the same high-fire warning once MODIS retires?")

            # ---- zoom into a range of monthly detections (most months are small, a few are huge)
            vmax = float(max(cc.real.max(), cc.mapped.max(), 1.0))
            step = float(10 ** int(np.floor(np.log10(vmax))))          # 10,000 when the biggest month is 14,000
            presets = {"All months": None}
            for k in range(int(np.ceil(vmax / step))):
                lo_ex, hi_k = (-1.0 if k == 0 else k * step), (k + 1) * step
                if ((cc.real > lo_ex) & (cc.real <= hi_k)).any():          # only offer ranges that contain months
                    presets[f"{0 if k == 0 else k * step + 1:,.0f} - {hi_k:,.0f}"] = (lo_ex, hi_k)
            presets["Custom"] = "custom"
            rc1, rc2 = st.columns([4, 1])
            choice = rc1.radio(f"Show months where real MODIS recorded ({U})", list(presets), horizontal=True, key="val_range",
                               help="Most months are small and a few are huge, so the big ones squash everything else. Pick a "
                                    "range to zoom in; the chart, the table and the ratio below all follow it.")
            log_scale = rc2.checkbox("Log scale", False, key="val_log",
                                     help="Spreads out small months without cutting anything off.")
            if choice == "All months":
                mask, view_lo, view_hi = pd.Series(True, index=cc.index), 0.0, None
            elif choice == "Custom":
                cu1, cu2 = st.columns(2)
                lo = cu1.number_input(f"From ({U})", min_value=0.0, value=0.0, step=step, key="val_lo")
                hi = cu2.number_input(f"To ({U})", min_value=0.0, value=float(np.ceil(vmax)), step=step, key="val_hi")
                lo, hi = (hi, lo) if lo > hi else (lo, hi)
                mask, view_lo, view_hi = (cc.real >= lo) & (cc.real <= hi), lo, hi
            else:
                lo_ex, hi = presets[choice]
                mask, view_lo, view_hi = (cc.real > lo_ex) & (cc.real <= hi), max(lo_ex, 0.0), hi
            sel, fr_s, fm_s = cc[mask], fr[mask], fm[mask]

            if sel.empty:
                st.info("No months fall in this range. Pick another range.")
            else:
                left_col, right_col = st.columns([1.25, 1])
                with left_col:
                    lim_hi = float(view_hi) if view_hi is not None else float(max(sel.real.max(), sel.mapped.max()))
                    lim_lo = float(view_lo)
                    y_lo = min(lim_lo, float(sel.mapped.min())) if view_hi is not None else 0.0
                    y_hi = max(lim_hi, float(sel.mapped.max())) * 1.05
                    kind = "symlog" if log_scale else "linear"
                    xs = alt.Scale(type=kind, domain=[lim_lo, lim_hi]) if view_hi is not None else alt.Scale(type=kind)
                    ys = alt.Scale(type=kind, domain=[y_lo, y_hi]) if view_hi is not None else alt.Scale(type=kind)
                    sc = (
                        alt.Chart(sel).mark_circle(size=70, opacity=0.75, color="#1f77b4", clip=True).encode(
                            x=alt.X("real:Q", scale=xs, title=f"Real MODIS ({U} per month)"),
                            y=alt.Y("mapped:Q", scale=ys, title=f"VIIRS mapped ({U} per month)"),
                            tooltip=[
                                alt.Tooltip("month:T", format="%b %Y", title="Month"),
                                alt.Tooltip("real:Q", format=",.0f", title="Real MODIS"),
                                alt.Tooltip("mapped:Q", format=",.0f", title="VIIRS mapped"),
                            ],
                        )
                        + alt.Chart(pd.DataFrame({"x": [lim_lo, lim_hi], "y": [lim_lo, lim_hi]})).mark_line(
                            color="#999999", strokeDash=[4, 3], clip=True
                        ).encode(x=alt.X("x:Q", scale=xs), y=alt.Y("y:Q", scale=ys))
                    )
                    st.markdown("**Predicted vs. real monthly activity** (dashed line = perfect match; dots below it = estimate too low)")
                    st.altair_chart(sc.properties(height=320), width="stretch")

                with right_col:
                    st.markdown("**Month-by-month comparison:**")
                    comp_tbl = pd.DataFrame({
                        "Month": sel["month"].dt.strftime("%b %Y"),
                        "Real MODIS": sel["real"].round(0),
                        "VIIRS Mapped": sel["mapped"].round(0),
                        "Diff": (sel["mapped"] - sel["real"]).round(0),
                        "Alert Match": ["Both high" if (r and m) else ("Both normal" if (not r and not m) else "Mismatch")
                                        for r, m in zip(fr_s, fm_s)],
                    })
                    st.dataframe(
                        comp_tbl,
                        hide_index=True,
                        height=320,
                        width="stretch",
                        column_config={
                            "Real MODIS": st.column_config.NumberColumn(format="%d"),
                            "VIIRS Mapped": st.column_config.NumberColumn(format="%d"),
                            "Diff": st.column_config.NumberColumn(format="%+d"),
                        },
                    )
                ratio_sel = sel.mapped.sum() / max(sel.real.sum(), 1e-9)
                st.caption(f"In this range: {len(sel)} of {len(cc)} months; the VIIRS-based estimate is {ratio_sel:.0%} of real MODIS "
                           f"({after[0]:%d/%m/%Y} to {(after[-1] + pd.offsets.MonthEnd(0)):%d/%m/%Y}). "
                           "Small samples: read as evidence, not proof.")
        elif CONT_FILE.exists() and modis_last is not None:
            st.info("No MODIS activity after the cutoff in this area.")
        else:
            st.info("Validation needs a demo build: run `python run_pipeline.py --indonesia --demo`.")

    with v_tab2:
        if bt and "bias_ratio" not in bt:
            st.markdown(f"**National benchmark across all of Indonesia** (simulated cutoff: `{meta.get('backtest_cutoff', '')}`):")
            t = pd.DataFrame(bt).T.rename(columns={
                "bias_ratio": "Predicted / Real MODIS",
                "corr_log": "Correlation (log scale)",
                "mape_pct": "Typical error (%)",
                "coverage_95": "Inside 95% band",
            })
            t.index = [METRICS[k]["label"] for k in t.index]
            st.dataframe(
                t[["Predicted / Real MODIS", "Correlation (log scale)", "Typical error (%)", "Inside 95% band"]],
                width="stretch",
                column_config={
                    "Predicted / Real MODIS": st.column_config.NumberColumn(format="%.2fx"),
                    "Correlation (log scale)": st.column_config.NumberColumn(format="%.2f"),
                    "Typical error (%)": st.column_config.NumberColumn(format="%.1f%%"),
                    "Inside 95% band": st.column_config.NumberColumn(format="%.2f"),
                },
            )
            st.caption("Ideal targets: 1.00x for Predicted / Real MODIS, close to 1.00 for Correlation, and 0.95 for "
                       "Inside 95% band. *Fire-days* under-predicts intense seasons, which is why *Hotspots detected* is the default.")
        else:
            st.info("National backtest summary not found in `meta.json`.")

    with v_tab3:
        s1, s2, s3 = st.columns(3)
        s1.markdown("**1. Hide post-cutoff MODIS**\n\nIn `--demo` mode, MODIS observations after the cutoff date are withheld from the model.")
        s2.markdown("**2. Predict from VIIRS alone**\n\nUsing overlap-period calibration, the model converts VIIRS detections into MODIS-equivalent counts.")
        s3.markdown("**3. Check against ground truth**\n\nPredictions are scored against the withheld MODIS data to verify volume and high-fire alerts.")

# ---------------------------------------------------------------- DOWNLOAD
with tab_dl:
    st.markdown(f"Everything below uses your current choices: {area_name}, {start_date:%d/%m/%Y} to {end_date:%d/%m/%Y}, measure {ML}.")
    rng = f"{r0}_{r1}"
    tag = slug(area_name)
    ms = anom[(anom.index >= d0) & (anom.index <= d1)]
    monthly = pd.DataFrame({"month": ms.index.strftime("%Y-%m"), f"{metric}": ms["value"].round(2), "lower_95": ms["lo"].round(2),
                            "upper_95": ms["hi"].round(2), "normal_median": ms["median"].round(2),
                            "times_normal": ms["ratio_to_median"].round(3), "flag": ms["flag"]})
    st.markdown("**1. Monthly summary**: one row per month for the whole area.")
    st.download_button(f"Download monthly summary ({len(monthly)} rows, CSV)", monthly.to_csv(index=False),
                       f"monthly_summary_{tag}_{rng}.csv", "text/csv", key="dl_monthly")
    st.markdown("**2. Map data (5 km cells by month)**: one row per cell and month with the harmonized values and their bands. "
                "Use it to rebuild the map in GIS software.")
    if st.button("Prepare map data", key="btn_cells"):
        cm = sub[(sub.month >= d0) & (sub.month <= d1)]
        st.download_button(f"Download map data ({len(cm):,} rows, CSV)", cm.to_csv(index=False), f"map_cells_{tag}_{rng}.csv",
                           "text/csv", key="dl_cells")
    st.markdown("**3. Raw hotspots**: every satellite detection in the area and months: sensor, satellite, latitude, longitude, date, "
                "time (UTC, HHMM), confidence, day/night, fire radiative power. Not harmonized; the same fire can appear in "
                f"several rows. Capped at {HOT_LIMIT:,} rows (oldest first).")
    if not HOT_DIR.exists():
        st.info("Run run_pipeline.py on real FIRMS files to enable this.")
    elif st.button("Prepare raw hotspots", key="btn_hot"):
        hot, truncated = collect_hotspots(start_date, end_day, region_geom, method != "Province" and mask_land)
        if truncated:
            st.warning(f"More than {HOT_LIMIT:,} hotspots: only the oldest {HOT_LIMIT:,} are included. Narrow the area or dates.")
        st.download_button(f"Download {len(hot):,} hotspots (CSV)", hot.to_csv(index=False), f"hotspots_{tag}_{rng}.csv", "text/csv", key="dl_hot")
    st.markdown("**4. Last 7 days of hotspots** for incident response is in the *Right now* tab.")

# ---------------------------------------------------------------- ABOUT
with tab_info:
    st.subheader("What the three measures mean")
    st.table(pd.DataFrame([{"Measure": v["label"], "Unit": v["unit"], "What it tells you": v["help"]} for v in METRICS.values()]))
    st.subheader("What 'harmonized' means")
    st.markdown("MODIS (1 km pixels, 2000 onward) is being retired; VIIRS (375 m pixels, 2012 onward) replaces it but sees more small "
                "fires. For each region and calendar month we measure the overlap years to learn how many MODIS-equivalent "
                "detections a VIIRS count corresponds to, then convert VIIRS to that scale so one continuous record exists. "
                "The result is *MODIS-equivalent activity*, not a literal count of fires or burned area.")
    st.subheader("Why the pipeline is run with --demo")
    st.markdown("Terra and Aqua (MODIS) are still flying, so today MODIS alone covers the whole record and there is nothing to "
                "bridge. `--demo` (same as `--modis-last 2022-12-01`) pretends MODIS stopped in Dec 2022: the model sees "
                "only data up to then, VIIRS fills the later months, and the real MODIS is kept aside to check the result. "
                "When MODIS really ends, set `--modis-last` to its last month.")
    st.subheader("Caveats")
    _bt2 = (meta.get("backtest") or {})
    _bt2 = _bt2.get(metric) if "bias_ratio" not in _bt2 else _bt2
    st.markdown(
        "- Bands are 95% bands from leave-one-year-out cross-validation." +
        (f" For this measure the real MODIS value fell inside the band {_bt2['coverage_95']:.0%} of the time and the predicted total was "
         f"{_bt2['bias_ratio']:.0%} of real MODIS." if _bt2 else "") + "\n"
        "- The VIIRS/MODIS relationship changes with fire intensity, so very intense seasons can be under-estimated.\n"
        "- Cell-level maps are not comparable across the MODIS-to-VIIRS switch: VIIRS spreads the same total over more, lighter cells.\n"
        "- MODIS orbits (Terra, Aqua) have drifted in recent years, which can shift observation times. Check NASA's notes.\n"
        "- Province outlines are Natural Earth (33 provinces; Kalimantan Utara and the new Papua provinces are not separate).\n"
        "- Offshore, volcano and static-source detections are removed (about 5% of rows); coastal fires flagged offshore are lost.\n"
        "- The grid is in degrees (about 5 km near the equator): fine for Indonesia.")
    st.subheader("Credits")
    st.markdown("Fire detections: NASA LANCE FIRMS (earthdata.nasa.gov/firms). Basemaps: Esri World Shaded Relief; optional NASA GIBS "
                "Blue Marble and OpenStreetMap. Boundaries and cities: Natural Earth. The small map credits are required by the tile "
                "providers' terms; only the Leaflet flag is hidden.")