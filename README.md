# Burn Activity Calendar

A burning-activity calendar for Indonesia built from NASA FIRMS active-fire hotspots. MODIS (2000 onward) and VIIRS (2012 onward) are joined into one MODIS-equivalent monthly series, so you can pick an area and a date range and see which months and years burned most, how the current month compares with the historical median, and download the numbers.

Built for the NASA Space Apps Challenge on a harmonized burning activity calendar.

**Live app:** https://cakrawala.streamlit.app
<!-- Demo video / screenshots: add a link here -->

## How it answers the challenge

| The challenge asks for | Where it is |
|---|---|
| Harmonized MODIS + VIIRS hotspot series | **Harmonization** tab and the *Trend against normal* chart |
| Select a region and a date range | Sidebar: province list, drawing on the map, or coordinates, plus `From` / `To` dates |
| Burning calendar with a color ramp | **History → Calendar** |
| Current month vs the historical median | **Right now**, and the *Compared with normal* calendar |
| Anomalies against multi-year averages | **History → Months that stood out** |
| Number of fires detected | *Hotspots detected* (default), *Fire-days* and *Fire intensity (FRP)* |
| Downloadable CSV of hotspots | **Download** tab |
| Interactive map (draw an area, enter lat/lon) | Map at the top of the page |

## Using the app

**Sidebar**
1. *Where*: Province list, Drawing on map, or Coordinates.
2. *When*: `From` and `To` dates in `DD/MM/YYYY` (default: the most recent 24 months; **Reset to full period** restores everything).
3. *What to measure*: Hotspots detected, Fire-days, or Fire intensity (FRP). *Advanced* options: hide cells over the sea, and the baseline years that define "normal".

**Map.** Draw a rectangle or polygon and everything below updates. Two styles: `Heatmap (smooth)` shows harmonized activity per ~5 km cell for all of Indonesia, and `Raw alerts (1 km)` shows every satellite detection per ~1 km cell (not harmonized, for areas up to about 4 million map pixels). Optional layers: cities (colored by burning within 25 km) and province outlines. You can also filter by calendar months (for example dry season only).

**Tabs**
- **Right now**: the current partial month against the same days of earlier years, with a last-7-days hotspot CSV.
- **History**
  - *Calendar*: green (fewer detections) through neutral (normal) to amber and red (higher, extreme), in both *Compared with normal* and *Amount* modes, with a Jan–Dec baseline table.
  - *Trend against normal*: the harmonized series against the monthly normal. When the demo is active, the held-back real MODIS record is drawn as a red dashed line.
  - *Months that stood out*: filterable anomaly table (period, difference vs normal, 95% band, ratio, severity) with a **Focus** button that zooms the map and calendar to that month.
- **Harmonization**: a plain-English verdict for the selected area, with three sub-tabs: *Selected Area* (predicted vs real MODIS, month by month), *All-Indonesia Score Card* (national backtest metrics), and *How Validation Works*.
- **Download**: monthly summary CSV, 5 km cell-by-month CSV, and raw hotspot CSV (capped at 300,000 rows).
- **About the data**: measure definitions, harmonization overview, caveats and credits.

## The demo: how harmonization is tested

Terra and Aqua (MODIS) are still flying, so the pipeline is run in `--demo` mode: it pretends MODIS ended in December 2022, lets VIIRS fill 2023 onward, and keeps the real MODIS aside to check the result. Everything after December 2022 in the calendar is therefore a VIIRS-based estimate, and the **Harmonization** tab shows how close it came. When MODIS really ends, pass its real last month with `--modis-last YYYY-MM-01`.

**Try it: October 2023 (El Niño season).** It falls after the simulated cutoff. Choose all provinces, measure *Hotspots detected*, dates 01/01/2023 to 31/10/2023. Read October 2023 in **History → Calendar** and in *Months that stood out*, then compare the estimate with the held-back real MODIS in the **Harmonization** tab.
<!-- Optional, once you have the app's numbers, add one line here:
     "Harmonized (VIIRS only): X times normal, rank N of M. Real MODIS: Y times normal." -->

## Rules the app applies (so numbers match everywhere)
- Starts July 2002 (Terra flew alone before Aqua joined). The baseline for "normal" starts a year later by default and uses real-MODIS years only, because mapped VIIRS years are biased low.
- The calendar and statistics use complete months only. The current partial month is handled by **Right now**, using raw alerts from one VIIRS satellite (SNPP, or NOAA-20 when SNPP's data is older), so the years compared are consistent.
- Persistent cells (flares, industry) are removed from the calendar and from the hotspot CSV.
- The hotspot CSV uses the same area and land mask as the calendar, and is streamed and capped at 300,000 rows. The hotspot files include the newest days; the calendar does not.
- Comparison to normal is `(value + 1) / (median + 1)`. A month is "unusual" only if its 95% band lies outside the interquartile range **and** it is at least 1.5x or at most 2/3 of the median.
- Calendar classes: under 1/2, 1/2 to 2/3, 2/3 to 1.5x (normal), 1.5 to 2x, 2 to 4x, over 4x. The map color scale is logarithmic.

## How it works

```
fire_*.csv (FIRMS)  ->  clean.py  ->  grid.py  ->  harmonize.py  ->  data/processed/  ->  app.py (Streamlit)
                        types, confidence,  5 km cell x    MODIS-equivalent   harmonized_monthly.parquet
                        NRT/archive merge   month totals   series + backtest  hotspots/year=YYYY/*.parquet
                                                                              meta.json (QC, backtest, last complete month)
```

| file | role |
|---|---|
| `clean.py` | read FIRMS CSVs, detect sensor, satellite and archive vs NRT, filter type and confidence, QC counters |
| `grid.py` | 0.05 degree cell by sensor by month aggregates, persistent-source removal |
| `harmonize.py` | calibration, splice and `backtest()` (95% bands from leave-one-year-out cross-validation) |
| `climatology.py`, `geo.py`, `app.py` | queries, calendar, anomalies, maps, downloads |
| `run_pipeline.py` | runs everything and writes `data/processed/*` |
| `smoke_test.py` | PASS/FAIL end-to-end check of pipeline and app on synthetic data |
| `synthetic.py`, `demo_query.py`, `inspect_raw.py`, `download.py` | helpers |

**Data contract: `harmonized_monthly.parquet`.** Columns `cell_id, lon, lat, month, source` and, for each of `fire_days`, `n_hotspots`, `frp_sum`: `<metric>`, `<metric>_lo`, `<metric>_hi`. `source` is `MODIS` or `VIIRS_mapped`. Months with no fire have no row (the app fills zeros). Any new harmonization method must keep this schema and `lo <= value <= hi`; `smoke_test.py` checks it.

## Run it yourself

```bash
pip install -r requirements.txt
python smoke_test.py            # end-to-end check on synthetic data in a temp folder
```

The processed data used by the live app is already in `data/processed/`, so `python -m streamlit run app.py` works right away (http://localhost:8501).

To rebuild from raw data (about 600 MB, not included), download from NASA FIRMS (https://firms.modaps.eosdis.nasa.gov/download/) for 95 to 141 E, 11 S to 6 N, from 2000-11-01 to the latest day:

- MODIS Collection 6.1: archive and near-real-time (`fire_archive_M-C61_*.csv`, `fire_nrt_M-C61_*.csv`)
- VIIRS S-NPP 375 m: archive and NRT (`fire_archive_SV-C2_*.csv`, `fire_nrt_SV-C2_*.csv`)
- VIIRS NOAA-20 375 m: archive and NRT (`fire_archive_J1V-C2_*.csv`, `fire_nrt_J1V-C2_*.csv`)

```bash
# put the CSVs in data/raw/
python inspect_raw.py                       # coverage and overlap check
python run_pipeline.py --indonesia --demo   # clean, grid, harmonize, backtest
python -m streamlit run app.py
```

`--demo` is shorthand for `--modis-last 2022-12-01`. `--backtest CUTOFF` defaults to the same month.

**Refreshing recent days (optional).** Get a free key at https://firms.modaps.eosdis.nasa.gov/api/map_key/ (5,000 requests per 10 minutes) and keep it out of git:

```bash
python download.py --key $FIRMS_MAP_KEY --source VIIRS_SNPP_NRT --bbox 94 -11.5 141.5 6.5 --start 2026-11-01 --end 2026-11-14
```
Then put the CSVs in `data/raw/` and rerun the pipeline. A non-CSV answer from the API (bad key, rate limit) raises an error instead of being skipped.

## Known limits
- **Validation (MODIS pretended to end Dec 2022, scored on 2023 to 2026).** About 86% of real monthly values fall inside the 95% bands for hotspots and fire-days, and 90% for FRP. Total prediction versus real MODIS is 89% (hotspots), 74% (fire-days) and 100% (FRP). Fire-days under-predict intense seasons, so hotspot count is the best-validated measure and the default.
- Calibrating on SNPP only was tested: fire-day bias improves (74% to 87%) but correlation drops (0.96 to 0.92), so it was not adopted.
- **Right now** compares one satellite against its own earlier years, so its baseline is shorter than the calendar's.
- Cell-level maps are not comparable across the MODIS-to-VIIRS switch.
- Province outlines are Natural Earth (33 provinces), so Kalimantan Utara and the newer Papua provinces are not separate areas. The land mask hides about 1.4% of activity nationally.
- FIRMS `type != 0` rows (volcano, static, offshore) are dropped, about 5% of rows, so coastal detections flagged offshore are lost.
- VIIRS NOAA-21 is not used. The grid is in degrees, which is fine near the equator.
- Not included: a forest-cover layer (needs the JRC GFC2020 raster).
- The hosted app runs on free infrastructure and may be slow or restart when many people use it at once.
- The color ramp runs from green to red, which is not ideal for red-green color blindness. Each class is also named in the legend.

## Attribution
NASA LANCE FIRMS (earthdata.nasa.gov/firms). Esri World Shaded Relief basemap (optional NASA GIBS Blue Marble and OpenStreetMap). Boundaries and cities: Natural Earth (public domain).