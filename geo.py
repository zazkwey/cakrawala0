"""Boundaries (Natural Earth, public domain), land mask, city points and the grid-raster overlay."""
import base64
import io
import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import shapely
from PIL import Image
from shapely.geometry import shape
from shapely.ops import unary_union

from config import CELL_ID_MULT, GRID_DEG

ASSETS = Path(__file__).resolve().parent / "assets"
# YlOrRd (ColorBrewer): sequential, colour-blind friendly
STOPS = np.array([[255, 255, 178], [254, 204, 92], [253, 141, 60], [240, 59, 32], [189, 0, 38]], dtype=float)


@lru_cache(maxsize=1)
def load_provinces() -> dict:
    gj = json.loads((ASSETS / "indonesia_provinces.geojson").read_text())
    return {f["properties"]["name"]: shape(f["geometry"]) for f in gj["features"]}


@lru_cache(maxsize=1)
def land_geometry():
    return unary_union(list(load_provinces().values()))


@lru_cache(maxsize=1)
def load_cities() -> list:
    gj = json.loads((ASSETS / "indonesia_cities.geojson").read_text())
    return [(f["properties"]["name"], f["properties"]["pop"], f["geometry"]["coordinates"][1],
             f["geometry"]["coordinates"][0]) for f in gj["features"]]


def assign_province(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Province name for each point, None where the point is not on land (sea)."""
    out = np.full(len(lon), None, dtype=object)
    for name, g in load_provinces().items():
        w, s, e, n = g.bounds
        cand = np.where((out == None) & (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n))[0]  # noqa: E711
        if len(cand):
            hit = shapely.contains_xy(g, lon[cand], lat[cand])
            out[cand[hit]] = name
    return out


def _render(v, x0, y0, deg, scale):
    """v[row, col] counts (row 0 = southernmost). Log colour scale; returns (png data URI, bounds, low, high)."""
    H, W = v.shape
    on = v > 0
    t = np.log1p(v)
    top = np.quantile(t[on], 0.995) if on.any() else 1.0
    t = np.clip(t / max(top, 1e-9), 0, 1) * (len(STOPS) - 1)
    lo = np.minimum(t.astype(int), len(STOPS) - 2)
    fr = (t - lo)[..., None]
    rgb = STOPS[lo] * (1 - fr) + STOPS[lo + 1] * fr
    rgba = np.zeros((H, W, 4), dtype=np.uint8)
    rgba[..., :3] = rgb.astype(np.uint8)
    rgba[..., 3] = np.where(on, 235, 0)
    img = Image.fromarray(rgba[::-1])                                    # north up
    if scale != 1:
        img = img.resize((W * scale, H * scale), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    bounds = [[y0 * deg - 90, x0 * deg - 180], [(y0 + H) * deg - 90, (x0 + W) * deg - 180]]
    return uri, bounds, float(v[on].min()) if on.any() else 0.0, float(np.expm1(top))


def grid_overlay(cells, metric, deg=GRID_DEG, scale=4):
    """Harmonized 5 km cells (identified by cell_id) -> coloured image overlay."""
    cid = cells["cell_id"].to_numpy().astype("int64")
    iy, ix = cid // CELL_ID_MULT, cid % CELL_ID_MULT
    x0, x1, y0, y1 = ix.min(), ix.max(), iy.min(), iy.max()
    v = np.zeros((y1 - y0 + 1, x1 - x0 + 1))
    v[iy - y0, ix - x0] = cells[metric].to_numpy()
    return _render(v, x0, y0, deg, scale)


DETAIL_DEG = 0.009          # about 1 km at the equator
DETAIL_MAX_PIXELS = 4_000_000


def count_overlay(counts, deg=DETAIL_DEG):
    """counts: DataFrame with integer columns ix, iy (floor((lon+180)/deg), floor((lat+90)/deg)) and n."""
    ix, iy = counts["ix"].to_numpy().astype("int64"), counts["iy"].to_numpy().astype("int64")
    x0, y0 = ix.min(), iy.min()
    v = np.zeros((iy.max() - y0 + 1, ix.max() - x0 + 1))
    v[iy - y0, ix - x0] = counts["n"].to_numpy()
    return _render(v, x0, y0, deg, 1)
