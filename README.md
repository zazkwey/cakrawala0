# Burn Activity Calendar: MODIS + VIIRS harmonized FIRMS hotspots (Indonesia first)

```
fire_*.csv (FIRMS)  ->  clean.py  ->  grid.py  ->  harmonize.py  ->  data/processed/  ->  app.py (Streamlit)
                        types, confidence,  5 km cell x    MODIS-equivalent   harmonized_monthly.parquet
                        NRT/archive merge   month totals   series (friend)    hotspots/year=YYYY/*.parquet
                                                                              meta.json (QC, backtest, last complete month)
```

## Quick start
```bash
pip install -r requirements.txt
python smoke_test.py            # end-to-end check on synthetic data in a temp folder (real data untouched)
```
Real data:
```bash
# copy your fire_archive_*.csv and fire_nrt_*.csv (MODIS + VIIRS) into data/raw/
python inspect_raw.py                                              # coverage + overlap check
python run_pipeline.py --indonesia --demo
python -m streamlit run app.py                                     # http://localhost:8501
```
`--demo` = `--modis-last 2022-12-01`: pretend MODIS ended in Dec 2022. The model only sees data up to then, VIIRS fills the later
months, and the real MODIS is kept aside to check the result (that is the backtest and the app's "Does it work?" tab).
Why: Terra and Aqua still fly, so without it the output is MODIS only and nothing is harmonized (the pipeline warns).
When MODIS really ends, pass its real last month: `--modis-last YYYY-MM-01`. `--backtest CUTOFF` defaults to the same month.

## Files
| file | role |
|---|---|
| `clean.py` | read FIRMS CSVs, auto-detect sensor/satellite/archive vs NRT, filter type + confidence, QC counters |
| `grid.py` | 0.05 deg cell x sensor x month aggregates, persistent-source removal |
| `harmonize.py` | calibration + splice + `backtest()` |
| `climatology.py`, `app.py`, `geo.py` | queries, calendar, anomalies, maps, downloads |
| `run_pipeline.py` | runs everything, writes `data/processed/*` |
| `smoke_test.py` | PASS/FAIL check of pipeline + app on synthetic data |
| `synthetic.py`, `demo_query.py`, `inspect_raw.py`, `download.py` | helpers |

## Contract: `harmonized_monthly.parquet`
`cell_id, lon, lat, month, source` and, for `fire_days`, `n_hotspots`, `frp_sum`: `<metric>`, `<metric>_lo`, `<metric>_hi`.
`source` is `MODIS` or `VIIRS_mapped`. Months with no fire have no row (the app fills zeros). Any new harmonization
method must keep this schema (and `lo <= value <= hi`; `smoke_test.py` checks it).

## The map
One map does everything: choose "Drawing on map", draw a rectangle or polygon, and the calendar, KPIs and map all update.
- **Map styles:** `Heatmap (smooth)` (harmonized MODIS-equivalent activity per ~5 km cell, works for all of Indonesia) and
  `Raw alerts (1 km)` (every satellite detection per ~1 km cell, not harmonized, for areas up to ~20 x 20 degrees).
- **Overlays & notes:** Optional city exposure markers (within 25 km) and province outlines. When sea-hiding is active for drawn
  areas or coordinate boxes, the dropped-activity note is shown directly under the **Map** header. Basemap attributions are
  compactly styled while keeping tile provider credits intact.
- Not included: forest cover layer (needs the JRC GFC2020 raster, not bundled).

## App layout
- **Sidebar:**
  1. *Where*: Province list, Drawing on map, or Coordinates.
  2. *When*: `From` and `To` date inputs in `DD/MM/YYYY` format (defaults to the most recent 24 months, with a one-click
     **Reset to full period** button).
  3. *What to measure*: Hotspots detected, Fire-days, or Fire intensity (FRP), plus *Advanced* options (sea mask and baseline years).
- **Map:** Interactive Folium map on top with optional calendar-month filtering (e.g., dry season only) and nearby-city exposure table.
- **Tabs:**
  - **Right now:** Current partial month vs. the same days of previous years (with full legend labels and a last-7-days hotspot CSV).
  - **History:**
    - *Calendar*: Green-to-red heatmap (green = fewer/lower detections, neutral = normal, amber/red = higher/extreme detections) for
      both *Compared with normal* and *Amount* modes, paired with a collapsible **Jan–Dec normal baseline & comparison** table.
    - *Trend against normal*: Harmonized series against the monthly normal, with a high-contrast red dashed overlay for the withheld
      real MODIS record when `--demo` / `--modis-last` is active.
    - *Months that stood out*: Filterable anomaly table (`DD/MM/YYYY` period bounds, signed difference vs. normal, 95% band, ratio,
      and severity status) with a one-click **Focus DD/MM/YYYY – DD/MM/YYYY** button to zoom the map and calendar to any standout month.
  - **Does it work?:** Opens with a plain-English verdict banner for the selected area, organized into three sub-tabs:
    1. *Selected Area* (KPIs, predicted-vs.-real scatter plot, and a side-by-side month-by-month comparison table),
    2. *All-Indonesia Score Card* (national backtest metrics), and
    3. *How Validation Works* (3-step explanation).
  - **Download:** Monthly summary CSV, 5 km cell-by-month map CSV, and raw hotspot CSV (capped at 300,000 rows).
  - **About the data:** Metric definitions, harmonization overview, caveats, and credits.

## Rules the app applies (so numbers match everywhere)
- Starts Jul 2002 (Terra flew alone before Aqua joined). Baseline for "normal" starts a year later by default.
- Defaults to a 24-month display window for readability; if fewer than 12 months are selected, the **History** tab prompts to expand
  the date range for multi-year seasonal and trend analysis.
- Calendar and statistics use complete months only (`meta.json: last_complete_month`); the current partial month is handled by the
  **Right now** tab with raw alerts from one VIIRS satellite (SNPP, or NOAA-20 when SNPP's data is older), so years are comparable.
- Persistent cells (flares/industry) removed from the calendar AND the hotspot CSV.
- Hotspot CSV uses the same area and land mask as the calendar, streamed and capped at 300,000 rows. The hotspot files include the
  newest days (the calendar does not).
- 'Normal' baseline defaults to the real-MODIS years only (mapped VIIRS years are biased low).
- Comparison to normal uses `(value + 1) / (median + 1)`. "Unusual" needs the 95% band outside the IQR AND `>= 1.5x` or `<= 2/3` of the median.

## Continuity check
With `--modis-last`, the pipeline keeps the real MODIS after that month aside (`continuity_modis.parquet`). The app overlays it as a
red dashed line on the VIIRS-mapped series for the selected area and reports whether the same months would be flagged 'high' from VIIRS alone.

## Note for the harmonization owner
`harmonize.py` changed: bands now come from leave-one-year-out cross-validation (`_cv_error_bands`), and `fit_ratio` /
`predict` were refactored around it. The output schema is unchanged. Merge this into your version before editing further.

## Known limits (also shown in the app's Caveats box)
- Bands are 95% bands from leave-one-year-out cross-validation. Out-of-time backtest (MODIS pretended to end Dec 2022, scored on 2023-2026):
  coverage 86% (hotspots, fire days) and 90% (FRP). Total prediction vs real MODIS: 89% (hotspots), 74% (fire days), 100% (FRP).
  So fire days under-predict intense seasons; hotspot count is the best validated metric and is the app's default.
- Calibrating on SNPP only was tested: fire-day bias improves (74% -> 87%) but correlation drops (0.96 -> 0.92). Not adopted.
- Cell-level maps are not comparable across the MODIS-to-VIIRS switch.
- Province outlines: Natural Earth, 33 provinces (no Kalimantan Utara / new Papua provinces). Land mask hides about 1.4% of activity nationally.
- FIRMS `type != 0` rows (volcano, static, offshore) are dropped, about 5% of rows; coastal detections flagged offshore are lost.
- Grid is in degrees: fine near the equator.

## Refreshing recent days (optional)
Free key: https://firms.modaps.eosdis.nasa.gov/api/map_key/ (5,000 requests / 10 min). Keep it out of git.
`python download.py --key $FIRMS_MAP_KEY --source VIIRS_SNPP_NRT --bbox 94 -11.5 141.5 6.5 --start 2026-11-01 --end 2026-11-14`
then drop the CSVs in `data/raw/` and rerun the pipeline. A non-CSV answer (bad key, rate limit) now raises instead of being skipped.

## Attribution
NASA LANCE FIRMS (earthdata.nasa.gov/firms). Esri World Shaded Relief basemap (optional NASA GIBS Blue Marble and OpenStreetMap).
Boundaries/cities: Natural Earth (public domain; verify).