"""Inspection helpers for the model: data inventory, attribute search, reference plots, geometry probes."""
from __future__ import annotations

import json
import re

import numpy as np

from .runctx import Run, read_json

ROLES = {
    "catchment": "Catchment / sub-catchment outline",
    "streams": "Stream network (named lines)",
    "stream_km": "Stream chainage / river-km markers",
    "roads": "Roads (centrelines)",
    "road_km": "Road kilometre markers",
    "road_land": "Road land / parcels (road width support)",
    "bridges": "Bridges",
    "buildings": "Building footprints (polygons)",
    "building_points": "Building points / addresses",
    "names": "Place, farm and field names (gazetteer)",
    "hazard_zones": "Hazard zone plans (reference only, never event outlines)",
    "dem": "Elevation model",
    "orthophoto": "Orthophoto (current or historical)",
    "landcover": "Land cover / land use",
}


def _layer_role(run: Run, name: str) -> str:
    for s in run.manifest():
        if s.get("name") == name and s.get("role"):
            return s["role"]
    n = name.lower()
    guesses = [("osm_buildings", "buildings"), ("building_points", "building_points"), ("catch", "catchment"), ("stream", "streams"),
               ("waterway", "streams"), ("river_km", "stream_km"), ("road_km", "road_km"), ("parcel", "road_land"), ("bridge", "bridges"),
               ("osm_roads", "roads"), ("road", "roads"), ("name", "names"), ("place", "names"), ("hazard", "hazard_zones"),
               ("osm_areas", "landcover")]
    return next((r for k, r in guesses if k in n), "")


def inventory(run: Run, write: bool = True) -> str:
    import pyogrio
    import rasterio

    rows, have = [], set()
    if run.gpkg.exists():
        for name, gtype in pyogrio.list_layers(run.gpkg):
            info = pyogrio.read_info(run.gpkg, layer=name)
            role = _layer_role(run, name)
            if role:
                have.add(role)
            if role == "buildings" and "Polygon" not in str(gtype):
                have.discard("buildings")
                have.add("building_points")
            rows.append(f"| {name} | {gtype} | {info['features']} | {role} | {', '.join(list(info['fields'])[:12])} |")
    rrows = []
    for name, rel in (run.config.get("rasters") or {}).items():
        p = run.path(rel)
        if not p.exists():
            continue
        with rasterio.open(p) as ds:
            rrows.append(f"| {name} | {ds.count} band(s) | {abs(ds.res[0]):.2f} m | {[round(v) for v in ds.bounds]} |")
        meta = next((s for s in run.manifest() if s.get("name") == name), {})
        have.add(meta.get("role") or ("orthophoto" if ds.count >= 3 else "dem"))
    lines = ["# Spatial data inventory", "", f"CRS {run.crs}; bbox {run.bbox()}; detail bbox {run.bbox('detail_bbox')}.", "",
             "Vector layers (data_raw/sources.gpkg):", "", "| Layer | Geometry | Features | Role | Fields |", "| --- | --- | --- | --- | --- |"]
    lines += rows or ["| (none) | | | | |"]
    lines += ["", "Rasters:", "", "| Name | Bands | Resolution | Bounds |", "| --- | --- | --- | --- |"] + (rrows or ["| (none) | | | |"])
    lines += ["", "Checklist:", ""]
    for role, label in ROLES.items():
        lines.append(f"- [{'x' if role in have else ' '}] {label}")
    failed = [s for s in run.manifest() if s.get("kind") == "failed"]
    if failed:
        lines += ["", "Failed downloads:", ""] + [f"- {s['name']}: {s.get('error', '')[:200]}" for s in failed]
    text = "\n".join(lines) + "\n"
    if write:
        (run.raw / "inventory.md").write_text(text, encoding="utf-8")
    return text


def find(run: Run, pattern: str, layer: str | None = None, near: tuple | None = None, radius: float = 500, limit: int = 60) -> list[dict]:
    from shapely.geometry import Point

    rx = re.compile(pattern, re.I)
    out = []
    for name in ([layer] if layer else run.layer_names()):
        try:
            g = run.layer(name)
        except Exception:
            continue
        if near:
            g = g[g.distance(Point(*near)) <= radius]
        cols = [c for c in g.columns if c != "geometry"]
        for idx, r in g.iterrows():
            hits = {c: r[c] for c in cols if r[c] is not None and str(r[c]) != "nan" and rx.search(str(r[c]))}
            if not hits and pattern not in (".", ".*"):
                continue
            geom = r.geometry
            c = geom.representative_point() if geom is not None else None
            out.append(dict(layer=name, fid_=r.get("fid_", idx), match=hits, geom_type=geom.geom_type if geom is not None else None,
                            x=round(c.x, 1) if c else None, y=round(c.y, 1) if c else None,
                            length_m=round(geom.length, 1) if geom is not None and geom.length else None,
                            area_m2=round(geom.area, 1) if geom is not None and geom.area else None))
            if len(out) >= limit:
                return out
    return out


def plot(run: Run, bbox, layers: list[str] | None = None, labels: list[str] | None = None, raster: str | None = None,
         refs: list[str] | None = None, parts: bool = False, out: str = "plot", grid: float | None = None, title: str = "") -> str:
    """Reference map PNG (tmp/plots) with labelled feature ids for visual matching and QA."""
    import geopandas as gpd
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import rasterio
    from rasterio.windows import from_bounds

    x0, y0, x1, y1 = bbox
    fig, ax = plt.subplots(figsize=(14, 14 * (y1 - y0) / max(x1 - x0, 1)) if (y1 - y0) < 2 * (x1 - x0) else (10, 16))
    if raster:
        path = run.raster_path(raster) if raster != "hillshade" else run.raster_path(run.config.get("default_dem", "dem"))
        with rasterio.open(path) as ds:
            win = from_bounds(x0, y0, x1, y1, ds.transform)
            scale = max(1.0, max(win.width, win.height) / 2500)
            a = ds.read(window=win, boundless=True, fill_value=0,
                        out_shape=(ds.count, max(1, int(win.height / scale)), max(1, int(win.width / scale))))
            ext = (x0, x1, y0, y1)
            if raster == "hillshade" or a.shape[0] == 1:
                z = a[0].astype(float)
                z[~np.isfinite(z)] = np.nanmin(z) if np.isfinite(z).any() else 0
                gy, gx = np.gradient(z, abs(ds.res[0]) * scale)
                slope = np.arctan(np.hypot(gx, gy))
                aspect = np.arctan2(-gx, gy)
                hs = np.clip(np.cos(np.radians(45)) * np.cos(slope) + np.sin(np.radians(45)) * np.sin(slope) * np.cos(np.radians(315) - aspect), 0, 1)
                ax.imshow(hs, cmap="gray", extent=ext, alpha=0.9)
                cs = ax.contour(np.linspace(x0, x1, z.shape[1]), np.linspace(y1, y0, z.shape[0]), z, levels=np.arange(np.floor(np.nanmin(z) / 50) * 50, np.nanmax(z), 50),
                                colors="saddlebrown", linewidths=0.3)
                ax.clabel(cs, fontsize=5, fmt="%d")
            else:
                ax.imshow(a[:3].transpose(1, 2, 0), extent=ext)
    from shapely.geometry import box as _box

    view = _box(x0, y0, x1, y1)
    palette = ["#0055ff", "#e69900", "#555555", "#aa00aa", "#008800", "#cc0000", "#00aaaa"]
    label_spec = dict(l.split(":", 1) if ":" in l else (l, "fid_") for l in (labels or []))
    for i, name in enumerate(layers or []):
        try:
            g = run.layer(name)
        except Exception as exc:
            print(exc)
            continue
        g = g[g.intersects(view)]
        if g.empty:
            continue
        col = palette[i % len(palette)]
        if g.geom_type.isin(["Polygon", "MultiPolygon"]).all():
            g.plot(ax=ax, facecolor="none", edgecolor=col, linewidth=0.8)
        elif g.geom_type.isin(["Point", "MultiPoint"]).all():
            g.plot(ax=ax, color=col, markersize=12)
        else:
            g.plot(ax=ax, color=col, linewidth=1.2)
        if name in label_spec:
            fld = label_spec[name]
            for _, r in g.iterrows():
                p = r.geometry.representative_point()
                if view.contains(p):
                    ax.annotate(str(r.get(fld, "")), (p.x, p.y), fontsize=7, color=col, ha="center",
                                bbox=dict(boxstyle="round,pad=0.1", fc="white", alpha=0.6, lw=0))
    if refs:
        from .geom import Evaluator

        spec = read_json(run.dir / "scripts" / "placements.json", {}) or {}
        evl = Evaluator(run, spec)
        for k, rname in enumerate(refs):
            g = evl.geom(json.loads(rname) if rname.strip().startswith("{") else rname)
            gs = gpd.GeoSeries([g], crs=run.crs)
            if g.geom_type in ("Polygon", "MultiPolygon"):
                gs.plot(ax=ax, facecolor="none", edgecolor="red", linewidth=1.5, linestyle="--")
            else:
                gs.plot(ax=ax, color="red", linewidth=2, markersize=40)
            c = g.representative_point()
            ax.annotate(rname[:30], (c.x, c.y), color="red", fontsize=8)
    if parts and (run.out / "segments.shp").exists():
        sg = gpd.read_file(run.out / "segments.shp", encoding="UTF-8")
        sg = sg[sg.intersects(view)]
        for k, (_, r) in enumerate(sg.iterrows()):
            gpd.GeoSeries([r.geometry]).plot(ax=ax, facecolor="none", edgecolor=plt.cm.tab20(k % 20), linewidth=1.4,
                                             linestyle="--" if r.is_hypo == 1 else "-")
            c = r.geometry.representative_point()
            ax.annotate(r.part_id, (c.x, c.y), fontsize=8, fontweight="bold", color=plt.cm.tab20(k % 20))
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    if grid:
        ax.set_xticks(np.arange(np.ceil(x0 / grid) * grid, x1, grid))
        ax.set_yticks(np.arange(np.ceil(y0 / grid) * grid, y1, grid))
        ax.grid(alpha=0.4)
        ax.tick_params(labelsize=6)
        plt.setp(ax.get_xticklabels(), rotation=90)
    ax.set_title(title or f"{run.crs} {bbox}", fontsize=9)
    run.plots.mkdir(parents=True, exist_ok=True)
    target = run.plots / (out if out.lower().endswith(".png") else f"{out}.png")
    fig.savefig(target, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(target)


def describe(run: Run, g) -> dict:
    c = g.representative_point() if not g.is_empty else None
    d = dict(type=g.geom_type, empty=g.is_empty, parts=len(getattr(g, "geoms", [g])), area_ha=round(g.area / 1e4, 6),
             length_m=round(g.length, 3), bounds=[round(v, 2) for v in g.bounds] if not g.is_empty else None,
             point=[round(c.x, 2), round(c.y, 2)] if c else None)
    if g.geom_type == "Point":
        try:
            d["elevation_m"] = round(run.dem().sample(g.x, g.y), 2)
        except Exception:
            pass
    if g.geom_type in ("LineString",):
        d["start"] = [round(v, 2) for v in g.coords[0]]
        d["end"] = [round(v, 2) for v in g.coords[-1]]
        try:
            dem = run.dem()
            d["start_elev"], d["end_elev"] = round(dem.sample(*g.coords[0]), 1), round(dem.sample(*g.coords[-1]), 1)
        except Exception:
            pass
    return d
