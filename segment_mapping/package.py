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


def load_package(path: str | Path, *, verify: bool = True) -> dict:
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
    if verify:
        preflight_package(pkg, raise_on_error=True)
    return pkg


def import_package(run: Run, path: str | Path) -> dict:
    """Import every layer/raster (one copy in the run CRS), record roles/fields in run.json and the manifest."""
    import geopandas as gpd

    from .sources import fetch_file

    pkg = load_package(path, verify=False)
    preflight_package(pkg, report_dir=run.path("outputs/spatial_preflight"), raise_on_error=True)
    if run.crs in (None, "EPSG:4326"):
        run.config["crs"] = pkg["crs"]
    _metre_crs(run.crs)
    run.config["crs_locked"] = True
    run.save_config()
    src_meta = {s["name"]: s for s in pkg.get("sources", [])}
    roles: dict = {}
    for lyr in pkg.get("layers", []):
        f = Path(pkg["_dir"]) / lyr["path"]
        g = gpd.read_file(f, layer=lyr.get("layer")) if lyr.get("layer") else gpd.read_file(f)
        if g.crs is None:
            raise PackageError(f"{lyr['name']}: dataset has no CRS")
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
    declared_dem = (pkg.get("preparation") or {}).get("default_dem") or pkg.get("default_dem")
    if declared_dem:
        run.config["default_dem"] = declared_dem
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


def _metre_crs(value):
    from pyproj import CRS

    crs = CRS.from_user_input(value)
    if not crs.is_projected or not crs.axis_info or any(
        abs(a.unit_conversion_factor - 1) > 1e-9 for a in crs.axis_info[:2]
    ):
        raise PackageError(f"CRS must be projected with metre axes: {value}")
    return crs


def _dem_coverage(path, catchment, catchment_crs):
    """Exact pixel-footprint areas, blockwise; outside pixels never enter the denominator."""
    import numpy as np
    import rasterio
    from rasterio.features import shapes
    from shapely.geometry import Polygon, shape
    import shapely
    from pyproj import Transformer

    with rasterio.open(path) as ds:
        if ds.crs is None:
            raise PackageError("DEM has no CRS")
        _metre_crs(ds.crs)
        cat = shapely.transform(catchment, Transformer.from_crs(catchment_crs, ds.crs, always_xy=True).transform, interleaved=False)
        def footprint_polygon(tr, width, height):
            return Polygon([(tr.a*x + tr.b*y + tr.c, tr.d*x + tr.e*y + tr.f)
                            for x, y in ((0, 0), (width, 0), (width, height), (0, height))])
        footprint = footprint_polygon(ds.transform, ds.width, ds.height)
        outside = cat.difference(footprint)
        covered_area = 0.0
        gaps = []
        gap_count = 0
        if not outside.is_empty:
            gaps.append(dict(kind="outside_raster", area_m2=outside.area, bounds=list(outside.bounds)))
            gap_count += 1
        valid_cells = nodata_cells = 0
        for _, window in ds.block_windows(1):
            tr = ds.window_transform(window)
            tile = footprint_polygon(tr, window.width, window.height)
            if not tile.intersects(cat):
                continue
            band = ds.read(1, window=window, masked=True)
            valid = (~np.ma.getmaskarray(band)) & np.isfinite(band.data)
            # Polygonization merges adjacent pixels before area intersection.
            for geom, value in shapes(valid.astype("uint8"), transform=tr):
                intersection = shape(geom).intersection(cat)
                if intersection.is_empty or intersection.area == 0:
                    continue
                if value:
                    covered_area += intersection.area
                else:
                    gap_count += 1
                    if len(gaps) < 100:
                        gaps.append(dict(kind="nodata", area_m2=intersection.area, bounds=list(intersection.bounds)))
            # Cell counts are diagnostic; coverage uses exact intersected areas above.
            from rasterio.features import geometry_mask
            inside = geometry_mask([cat.__geo_interface__], band.shape, tr, invert=True)
            valid_cells += int((valid & inside).sum())
            nodata_cells += int((~valid & inside).sum())
        fraction = min(1.0, max(0.0, covered_area / cat.area))
        return dict(actual_crs=ds.crs.to_string(), nodata=ds.nodata if ds.nodata is None or np.isfinite(ds.nodata) else str(ds.nodata),
                    resolution=list(ds.res), catchment_area_m2=cat.area, valid_area_m2=covered_area,
                    gap_area_m2=max(0.0, cat.area - covered_area), coverage_fraction=fraction,
                    valid_cells=valid_cells, nodata_cells=nodata_cells, gap_count=gap_count,
                    gaps=gaps, gaps_truncated=gap_count > len(gaps), method="pixel footprint intersection with catchment")


def preflight_package(package, *, report_dir=None, raise_on_error=False, min_dem_coverage=0.99):
    """Validate actual data. Return JSON-safe diagnostics; optionally write .json/.txt and raise.

    Mandatory roles are always required. Geographic source data may be reprojected,
    but the package target CRS and DEM must have projected metre axes. No CRS is inferred.
    """
    import geopandas as gpd
    from shapely.ops import unary_union

    if not 0 <= min_dem_coverage <= 1:
        raise ValueError("min_dem_coverage must be between zero and one")
    report = dict(ok=False, errors=[], warnings=[], layers=[], rasters=[])
    try:
        pkg = load_package(package, verify=False) if not isinstance(package, dict) else package
        _metre_crs(pkg.get("crs"))
    except (ValueError, OSError, KeyError) as exc:
        report["errors"].append(str(exc))
        pkg = None
    vectors = {}
    if pkg:
        dem_names = [i["name"] for i in pkg.get("rasters", []) if i.get("role") == "dem"]
        primary_dem = (pkg.get("preparation") or {}).get("default_dem") or pkg.get("default_dem") or next(iter(dem_names), None)
        report["primary_dem"] = primary_dem
        if primary_dem not in dem_names:
            report["errors"].append("declared primary DEM is not a DEM raster in this package")
        roles = {x.get("role") for x in pkg.get("layers", []) + pkg.get("rasters", [])}
        for role in REQUIRED_ROLES:
            if role not in roles:
                report["errors"].append(f"missing mandatory role: {role}")
        for role, items in (("catchment", pkg.get("layers", [])), ("streams", pkg.get("layers", [])), ("dem", pkg.get("rasters", []))):
            if not any(i.get("role") == role for i in items):
                report["errors"].append(f"mandatory role {role} must be a {'raster' if role == 'dem' else 'vector'} input")
        for item in pkg.get("layers", []):
            entry = dict(name=item["name"], role=item.get("role"))
            report["layers"].append(entry)
            try:
                path = Path(pkg.get("_dir", ".")) / item["path"]
                g = gpd.read_file(path, **({"layer": item["layer"]} if item.get("layer") else {}))
                if g.crs is None:
                    raise PackageError("dataset has no CRS; supply correctly georeferenced data")
                entry.update(actual_crs=g.crs.to_string(), count=len(g))
                if len(g) == 0 or g.geometry.isna().any() or g.geometry.is_empty.any():
                    raise PackageError("empty layer or missing/empty geometries")
                if not g.geometry.is_valid.all():
                    raise PackageError(f"{int((~g.geometry.is_valid).sum())} invalid geometries")
                allowed = {"catchment": {"Polygon", "MultiPolygon"}, "streams": {"LineString", "MultiLineString"}}
                if item.get("role") in allowed and not set(g.geom_type).issubset(allowed[item["role"]]):
                    raise PackageError(f"wrong geometry types for {item['role']}: {sorted(set(g.geom_type))}")
                projected = g.to_crs(pkg["crs"])
                if not projected.geometry.is_valid.all():
                    from shapely.validation import explain_validity

                    invalid = projected.geometry[~projected.geometry.is_valid]
                    details = "; ".join(explain_validity(geom) for geom in invalid.iloc[:3])
                    raise PackageError(f"{len(invalid)} invalid geometries after reprojection to {pkg['crs']}: {details}; "
                                       "repair and verify a projected copy before localization")
                vectors[item["name"]] = projected
            except Exception as exc:
                report["errors"].append(f"{item['name']}: {exc}")
        cats = [vectors[i["name"]].geometry.union_all() for i in pkg.get("layers", [])
                if i.get("role") == "catchment" and i["name"] in vectors]
        cat = unary_union(cats) if cats else None
        for item in pkg.get("rasters", []):
            entry = dict(name=item["name"], role=item.get("role"))
            report["rasters"].append(entry)
            try:
                import rasterio
                path = Path(pkg.get("_dir", ".")) / item["path"]
                with rasterio.open(path) as ds:
                    if ds.crs is None:
                        raise PackageError("raster has no CRS")
                    entry.update(actual_crs=ds.crs.to_string(), width=ds.width, height=ds.height)
                    if ds.count < 1 or ds.width < 1 or ds.height < 1:
                        raise PackageError("empty raster")
                if item.get("role") == "dem":
                    if cat is None or cat.is_empty or cat.area <= 0:
                        raise PackageError("DEM coverage cannot be verified without valid catchment polygons")
                    entry.update(_dem_coverage(path, cat, pkg["crs"]))
                    entry["is_primary"] = item["name"] == primary_dem
                    if entry["is_primary"] and entry["coverage_fraction"] + 1e-9 < min_dem_coverage:
                        raise PackageError(f"DEM covers {entry['coverage_fraction']:.2%} of catchment; required {min_dem_coverage:.2%}; gap {entry['gap_area_m2']:.1f} m2")
                    if entry["gap_area_m2"] > 1e-6:
                        report["warnings"].append(f"{item['name']}: catchment DEM coverage gaps recorded; " +
                                                  ("primary DEM" if entry["is_primary"] else "supplemental DEM, usable only within its valid coverage"))
            except Exception as exc:
                report["errors"].append(f"{item['name']}: {exc}")
        # Diagnostics only: endpoint degrees do not prove hydrologic connectivity/direction.
        for item in pkg.get("layers", []):
            if item.get("role") != "streams" or item["name"] not in vectors:
                continue
            from collections import Counter
            lines = list(vectors[item["name"]].explode(index_parts=False).geometry)
            endpoints = Counter(tuple(p)[:2] for line in lines for p in (line.coords[0], line.coords[-1]))
            diagnostic = dict(name=item["name"], line_count=len(lines), endpoint_nodes=len(endpoints),
                              dangling_endpoints=sum(n == 1 for n in endpoints.values()),
                              junction_endpoints=sum(n > 2 for n in endpoints.values()),
                              closed_lines=sum(line.is_ring for line in lines),
                              orientation="unverified: coordinate order is not evidence of downstream direction",
                              topology_method="exact shared endpoints; interior intersections and near misses are not snapped")
            if cat is not None:
                diagnostic["lines_intersecting_catchment"] = sum(line.intersects(cat) for line in lines)
                if not diagnostic["lines_intersecting_catchment"]:
                    report["errors"].append(f"{item['name']}: no streams intersect the catchment")
            report.setdefault("streams", []).append(diagnostic)
    report["ok"] = not report["errors"]
    if report_dir is not None:
        dest = Path(report_dir)
        write_json(dest / "preflight.json", report)
        lines = [f"Spatial preflight: {'PASS' if report['ok'] else 'FAIL'}"]
        lines += [f"ERROR: {e}" for e in report["errors"]] + [f"WARNING: {e}" for e in report["warnings"]]
        for r in report["rasters"]:
            if "coverage_fraction" in r:
                lines.append(f"{r['name']}: valid catchment coverage {r['coverage_fraction']:.2%}; gaps {r['gap_area_m2']:.1f} m2; CRS {r['actual_crs']}")
        for stream in report.get("streams", []):
            lines.append(f"{stream['name']}: {stream['line_count']} stream lines; {stream['dangling_endpoints']} dangling endpoints; {stream['junction_endpoints']} endpoint junctions; {stream['orientation']}")
        (dest / "preflight.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if raise_on_error and not report["ok"]:
        raise PackageError("Spatial package preflight failed: " + "; ".join(report["errors"]))
    return report
