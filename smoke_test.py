"""End-to-end check on SYNTHETIC data in a temp folder (your real data/ is never touched).

    python smoke_test.py

Runs: synthetic raw FIRMS files -> run_pipeline -> checks the outputs -> drives the Streamlit app headlessly.
Exit code 0 = everything passed.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="firms_smoke_")
os.environ["FIRMS_DATA_DIR"] = TMP            # must be set before any project module is imported
env = {**os.environ, "FIRMS_DATA_DIR": TMP}
failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run(*args):
    r = subprocess.run([sys.executable, *args], cwd=HERE, env=env, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr)[-600:]


ok, out = run("synthetic.py", "raw")
check("synthetic raw files written", ok, out)
ok, out = run("run_pipeline.py", "--modis-last", "2022-12-01", "--backtest", "2021-12-01")
check("pipeline runs", ok, out)

import pandas as pd  # noqa: E402

proc = Path(TMP) / "processed"
h = pd.read_parquet(proc / "harmonized_monthly.parquet")
meta = json.loads((proc / "meta.json").read_text())
check("harmonized has MODIS and VIIRS_mapped rows", set(h.source) == {"MODIS", "VIIRS_mapped"}, str(set(h.source)))
check("no missing coordinates or values", not h[["lon", "lat", "fire_days", "fire_days_lo", "fire_days_hi"]].isna().any().any())
check("bounds ordered lo <= value <= hi", bool(((h.fire_days_lo <= h.fire_days + 1e-6) & (h.fire_days <= h.fire_days_hi + 1e-6)).all()))
check("mapped VIIRS only after the cutoff", h[h.source == "VIIRS_mapped"].month.min() > pd.Timestamp("2022-12-01"))
check("meta.json has qc + per-metric backtest", "qc" in meta and set(meta.get("backtest") or {}) == {"fire_days", "n_hotspots", "frp_sum"})
check("continuity file (real MODIS after cutoff) written", (proc / "continuity_modis.parquet").exists())
check("backtest bands are not absurd (coverage 50%-100%)", all(0.5 <= v["coverage_95"] <= 1.0 for v in meta["backtest"].values()),
      str({k: round(v["coverage_95"], 2) for k, v in meta["backtest"].items()}))
check("persistent flare cell removed", meta["qc"].get("persistent_cells_dropped", 0) >= 1)
check("hotspot files exist", any((proc / "hotspots").rglob("*.parquet")))

from streamlit.testing.v1 import AppTest  # noqa: E402


def app_ok(label, at):
    check(label, not at.exception, "; ".join(str(e.message)[:120] for e in at.exception))


at = AppTest.from_file(str(HERE / "app.py"), default_timeout=300).run()
app_ok("app starts (Province mode, all)", at)
check("history KPIs shown", any(m.label.startswith("Latest complete month") for m in at.metric))
check("right-now panel computed (partial month)", any(m.label.startswith("Alerts, 1-") for m in at.metric),
      "no 'Alerts, 1-D' metric: partial-month path not exercised")
check("tabs present", len(at.tabs) >= 5)
areas = at.multiselect(key="areas")
kal = [o for o in areas.options if "Kalimantan" in o][:1]
at = areas.set_value(kal).run()
app_ok("province selection", at)
sl = at.select_slider(key="date_range")
at = sl.set_range(sl.options[-24], sl.options[-1]).run()
app_ok("date range", at)
at = at.radio(key="cal_mode").set_value("Compared with normal").run()
app_ok("calendar: compared with normal", at)
at = at.radio(key="cal_mode").set_value("Amount").run()
app_ok("calendar: amount", at)
at = at.radio(key="map_style").set_value("Raw alerts (1 km)").run()
app_ok("raw alerts (1 km) map style", at)
at = at.radio(key="map_style").set_value("Heatmap (smooth)").run()
app_ok("heatmap map style", at)
at = at.radio(key="flag_pick").set_value("Both").run()
app_ok("months that stood out: both", at)
for m in ["fire_days", "frp_sum", "n_hotspots"]:
    at = at.selectbox(key="metric").set_value(m).run()
    app_ok(f"measure {m}", at)
at = at.button(key="btn_hot").click().run()
app_ok("raw hotspot export", at)
at = at.button(key="btn_week").click().run()
app_ok("last-7-days export", at)
at = at.button(key="btn_cells").click().run()
app_ok("map-data export", at)
at = at.radio(key="method").set_value("Coordinates").run()
app_ok("coordinates mode", at)
at = at.radio(key="method").set_value("Drawing on map").run()
app_ok("drawing mode (no shape)", at)
box_ = h[h.source == "MODIS"]
x0, x1, y0, y1 = box_.lon.quantile(.2), box_.lon.quantile(.8), box_.lat.quantile(.2), box_.lat.quantile(.8)
at.session_state["drawn_polygon"] = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
at = at.run()
app_ok("drawn polygon drives page", at)
at = at.radio(key="map_style").set_value("Raw alerts (1 km)").run()
app_ok("drawn polygon + raw alerts", at)
at.session_state["drawn_polygon"] = [(0.0, 0.0), (0.1, 0.0), (0.1, 0.1), (0.0, 0.1)]     # drawn over empty sea
at = at.run()
app_ok("drawing over empty area does not kill the page", at)

# app must also work when only the synthetic harmonized file exists (no meta.json, no hotspots)
(proc / "meta.json").unlink()
at = AppTest.from_file(str(HERE / "app.py"), default_timeout=300).run()
app_ok("app works without meta.json", at)

print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
