"""Spatial package: a JSON manifest of provided local layers/rasters with roles and name fields.

{
  "name": "...", "crs": "EPSG:31254",
  "layers":  [{"name": "streams", "path": "streams.gpkg", "layer": null, "role": "streams",
               "name_field": "GEW_NAME_HAUPT", "id_field": "HZB_GEW", "ref_field": null, "value_field": null}],
  "rasters": [{"name": "dem", "path": "dem.tif", "role": "dem"}],
  "sources": [{"name": "streams", "provider": "...", "licence": "...", "date": "...", "url": "..."}]
}
Paths are relative to the manifest file. Required roles: catchment, streams, dem.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .runctx import Run, write_json

REQUIRED_ROLES = ("catchment", "streams", "dem")
LAYER_KEYS = ("name_field", "id_field", "ref_field", "value_field")
# name/id fields of the Tirol preset layers (used by export)
TIROL_FIELDS = {
    "catchments": dict(role="catchment", name_field="NAMEEZGWLV", id_field="HZBEZGWLV"),
    "streams": dict(role="streams", name_field="GEW_NAME_HAUPT", id_field="HZB_GEW"),
    "roads": dict(role="roads", name_field="NAME", ref_field="STR_CODE"),
    "local_roads": dict(role="roads"),
    "road_km": dict(role="road_km", value_field="KM1", ref_field="STR_CODE"),
    "bridges": dict(role="bridges", name_field="NAME", ref_field="STR_CODE"),
    "road_parcels": dict(role="road_land"),
    "places": dict(role="names", name_field="NAME"),
    "field_names": dict(role="names", name_field="NAME"),
    "building_addresses": dict(role="names", name_field="HOFNAME"),
    "osm_buildings": dict(role="buildings", name_field="name", id_field="osm_id"),
    "osm_roads": dict(role="roads", name_field="name", id_field="osm_id"),
    "osm_named": dict(role="names", name_field="name", id_field="osm_id"),
    "hazard_zones": dict(role="hazard_zones"),
}


class PackageError(ValueError):
    pass


def load_package(path: str | Path) -> dict:
    p = Path(path).resolve()
    pkg = json.loads(p.read_text(encoding="utf-8"))
    pkg["_dir"] = str(p.parent)
    if not pkg.get("crs"):
        raise PackageError("package needs a 'crs' (the local projected CRS, e.g. EPSG:31254)")
    roles = {x.get("role") for x in pkg.get("layers", [])} | {x.get("role") for x in pkg.get("rasters", [])}
    missing = [r for r in REQUIRED_ROLES if r not in roles]
    if missing:
        raise PackageError(f"package lacks required roles {missing} (have {sorted(r for r in roles if r)})")
    names = [x["name"] for x in pkg.get("layers", []) + pkg.get("rasters", [])]
    if len(names) != len(set(names)):
        raise PackageError(f"duplicate names in package: {names}")
    for x in pkg.get("layers", []) + pkg.get("rasters", []):
        f = Path(pkg["_dir"]) / x["path"]
        if not f.exists():
            raise PackageError(f"{x['name']}: file not found {f}")
    return pkg


def import_package(run: Run, path: str | Path) -> dict:
    """Import every layer/raster (one copy in the run CRS), record roles/fields in run.json and the manifest."""
    import geopandas as gpd

    from .sources import fetch_file

    pkg = load_package(path)
    if run.crs in (None, "EPSG:4326"):
        run.config["crs"] = pkg["crs"]
    run.config["crs_locked"] = True
    run.save_config()
    src_meta = {s["name"]: s for s in pkg.get("sources", [])}
    roles: dict = {}
    for lyr in pkg.get("layers", []):
        f = Path(pkg["_dir"]) / lyr["path"]
        g = gpd.read_file(f, layer=lyr.get("layer")) if lyr.get("layer") else gpd.read_file(f)
        if g.crs is None:
            g = g.set_crs(pkg["crs"])
        n = run.write_layer(lyr["name"], g)
        meta = src_meta.get(lyr["name"], {})
        run.record_source(dict(name=lyr["name"], kind="vector", provider=meta.get("provider", "provided with the task"),
                               url=meta.get("url", str(f)), layer=lyr.get("layer") or f.name, count=n, role=lyr["role"],
                               date_or_version=meta.get("date", ""), licence=meta.get("licence", ""),
                               licence_url=meta.get("licence_url", ""), used_for=meta.get("use", lyr["role"]), provided=True))
        roles[lyr["name"]] = {k: lyr.get(k) for k in ("role",) + LAYER_KEYS}
    run = Run(run.dir)  # reload: write_layer/record_source do not touch run.json
    for r in pkg.get("rasters", []):
        f = Path(pkg["_dir"]) / r["path"]
        meta = src_meta.get(r["name"], {})
        rec = fetch_file(run, r["name"], str(f), provider=meta.get("provider", "provided with the task"), licence=meta.get("licence", ""),
                         licence_url=meta.get("licence_url", ""), use=meta.get("use", r["role"]), date_note=meta.get("date", ""))
        rec.update(role=r["role"], provided=True)
        run.record_source(rec)
        run = Run(run.dir)
        if r["role"] == "dem" and not run.config.get("default_dem"):
            run.config["default_dem"] = r["name"]
        roles[r["name"]] = {"role": r["role"]}
    run.config["package"] = {"name": pkg.get("name", ""), "path": str(Path(path).resolve()), "layers": roles}
    if not run.config.get("bbox"):
        cat = next(n for n, v in roles.items() if v["role"] == "catchment")
        x0, y0, x1, y1 = [float(v) for v in run.layer(cat).total_bounds]
        run.config["bbox"] = [round(x0 - 1000, 1), round(y0 - 1000, 1), round(x1 + 1000, 1), round(y1 + 1000, 1)]
    run.save_config()
    return run.config["package"]


def export_package(run: Run, out_dir: str | Path, name: str = "") -> Path:
    """Write the run's data as a spatial package (one GeoPackage per layer + rasters + package.json)."""
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = {m["name"]: m for m in run.manifest()}
    known = (run.config.get("package") or {}).get("layers", {})
    layers, rasters, sources = [], [], []
    for lname in run.layer_names():
        if lname.startswith("digitized:"):
            continue
        info = dict(known.get(lname) or TIROL_FIELDS.get(lname) or {})
        role = info.get("role") or manifest.get(lname, {}).get("role") or ""
        if not role:
            continue
        g = run.layer(lname)
        g.drop(columns=[c for c in ("fid_",) if c in g.columns]).to_file(out / f"{lname}.gpkg", driver="GPKG", engine="pyogrio")
        layers.append({"name": lname, "path": f"{lname}.gpkg", "role": role, **{k: info.get(k) for k in LAYER_KEYS if info.get(k)}})
    for rname, rel in (run.config.get("rasters") or {}).items():
        m = manifest.get(rname, {})
        role = m.get("role") or ("dem" if rname.startswith("dem") else "orthophoto")
        shutil.copy2(run.path(rel), out / f"{rname}.tif")
        rasters.append({"name": rname, "path": f"{rname}.tif", "role": role})
    for item in layers + rasters:
        m = manifest.get(item["name"], {})
        osm = manifest.get("osm", {}) if item["name"].startswith("osm_") else {}
        sources.append({"name": item["name"], "provider": m.get("provider") or osm.get("provider", ""),
                        "url": m.get("url") or osm.get("url", ""), "licence": m.get("licence") or osm.get("licence", ""),
                        "licence_url": m.get("licence_url") or osm.get("licence_url", ""),
                        "date": m.get("date_or_version") or osm.get("date_or_version", ""), "use": m.get("used_for", "")})
    default = run.config.get("default_dem")
    if default:
        rasters.sort(key=lambda r: r["name"] != default)
    pkg = {"name": name or run.config.get("event", ""), "crs": run.crs, "layers": layers, "rasters": rasters, "sources": sources}
    write_json(out / "package.json", pkg)
    return out / "package.json"
