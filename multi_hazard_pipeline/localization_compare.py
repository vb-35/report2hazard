"""Compare localizations of the same report: pipeline runs and imported agent runs, side by side.

Every run is read into one common form: a status and reason per segment, and the mapped parts as WGS84 GeoJSON
with the same properties (role, hypothesis flag, confidences, area, method, quotes, reasoning). Pipeline runs are
read from their localization outputs; agent runs (an agent localizing a report on its own) are imported as a zip of
outputs in the format below and attached to the pipeline run whose report, segments and GIS data they used.

Agent output format (all in the zip, folders inside it are ignored):
- placements.gpkg, layer "parts": one feature per mapped part with seg_id, part_id, label, geometry_role
  (event_extent | location | search_area), is_hypo, event/location/boundary_confidence, method, area_ha,
  report_area_ha
- segment_results.csv: seg_id, status (located | unresolved), parts, reason
- segments_evidence.csv: seg_id, part_id, quotes, reasoning, uncertainty, alternatives
- optional: report.md, segments_map.html
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import shutil
import zipfile
from pathlib import Path
from typing import Any

from .core import read_json
from .errors import PipelineError
from .pipeline import new_run_id, save_manifest, utc_now

AGENT_KIND = "agent_localization"
AGENT_FILES = {"placements.gpkg": True, "segment_results.csv": True, "segments_evidence.csv": True,
               "report.md": False, "segments_map.html": False}
MAX_COMPARED = 12
COLORS = ["#e8590c", "#7048e8", "#1c7ed6", "#2b8a3e", "#c2255c", "#0c8599", "#5c940d", "#862e9c",
          "#d6336c", "#495057", "#f08c00", "#1864ab"]
ROLES = ("event_extent", "location", "search_area")

csv.field_size_limit(10**9)


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _csv_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _is_agent(manifest: dict) -> bool:
    return manifest.get("kind") == AGENT_KIND


def signature(segments: list[dict]) -> str:
    """Identify a report's segment set: runs compare only when they localized the same segments."""
    key = [[str(s.get("seg_id", "")).strip(), " ".join(str(s.get("process", "")).split()).casefold()] for s in segments]
    return hashlib.sha1(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _localization_config(run_dir: Path) -> dict | None:
    path = run_dir / "localization" / "run.json"
    return read_json(path) if path.is_file() else None


def _pipeline_entry(run_dir: Path, manifest: dict) -> dict | None:
    config = _localization_config(run_dir)
    if not config or not (run_dir / "localization" / "outputs" / "segments.geojson").is_file():
        return None
    segments = config.get("segments") or []
    return dict(run_id=run_dir.name, base_run_id=run_dir.name, kind="pipeline",
                model=f"Pipeline {_when(manifest.get('created_at', ''))} · {config.get('model') or 'model not recorded'}",
                status=(manifest.get("localization") or {}).get("status") or "",
                event=config.get("event") or manifest.get("event") or "", segments=len(segments),
                signature=signature(segments), created_at=manifest.get("created_at", ""))


def comparable_groups(root: Path) -> list[dict]:
    """Reports (same event and same segments) with all their runs, newest report first.

    An agent run belongs to the report it was imported for. Its segments and GIS context come from that report's
    pipeline run; if that run was deleted, another pipeline run of the same report takes its place.
    """
    manifests = {}
    for path in root.glob("*/manifest.json"):
        try:
            manifests[path.parent.name] = read_json(path)
        except (OSError, ValueError):
            continue
    groups: dict[str, dict] = {}
    pipeline: dict[str, dict] = {}
    for run_id, manifest in manifests.items():
        if _is_agent(manifest):
            continue
        try:
            entry = _pipeline_entry(root / run_id, manifest)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if entry is None:
            continue
        pipeline[run_id] = entry
        groups.setdefault(entry["signature"], dict(signature=entry["signature"], event=entry["event"],
                                                   segments=entry["segments"], runs=[]))["runs"].append(entry)
    for group in groups.values():
        newest = max(group["runs"], key=lambda run: run["created_at"])
        group["base_run_id"] = newest["run_id"]
    for run_id, manifest in manifests.items():
        if not _is_agent(manifest):
            continue
        agent = manifest.get("agent") or {}
        base = pipeline.get(str(agent.get("base_run_id", "")))
        sig = agent.get("signature") or (base or {}).get("signature")
        group = groups.get(sig)
        if group is None or not (root / run_id / "localization" / "outputs" / "placements.gpkg").is_file():
            continue
        group["runs"].append(dict(run_id=run_id, base_run_id=base["run_id"] if base else group["base_run_id"],
                                  kind="agent", model=agent.get("model") or "agent", status="imported",
                                  event=group["event"], segments=group["segments"], signature=sig,
                                  created_at=manifest.get("created_at", "")))
    for group in groups.values():
        # Pipeline runs first (newest first), then the agent runs by model name.
        group["runs"].sort(key=lambda run: (run["kind"] != "pipeline",
                                            "" if run["kind"] != "pipeline" else _desc(run["created_at"]), run["model"].casefold()))
        group["latest"] = max(run["created_at"] for run in group["runs"])
    return sorted(groups.values(), key=lambda group: group["latest"], reverse=True)


def _when(created_at: str) -> str:
    """Short date and time of a run (UTC), so runs of the same model can be told apart."""
    return created_at[:16].replace("T", " ") if created_at else "date unknown"


def _desc(text: str) -> str:
    """Sort key that orders ISO timestamps newest first."""
    return "".join(chr(0x10FFFF - ord(c)) for c in text)


def _entries(root: Path) -> dict[str, dict]:
    return {run["run_id"]: run for group in comparable_groups(root) for run in group["runs"]}


# --------------------------------------------------------------------------- reading outputs

def _part(props: dict, evidence: dict | None) -> dict:
    evidence = evidence or {}

    def get(*keys):
        for key in keys:
            value = _clean(evidence.get(key)) or _clean(props.get(key))
            if value:
                return value
        return ""

    area = get("area_ha")
    try:
        area = f"{float(area):.2f}"
    except ValueError:
        pass
    role = get("geometry_role")
    return dict(seg=_clean(props.get("seg_id")), part=get("part_id"), label=get("label", "part_label"),
                role=role if role in ROLES else "location",
                hypo=get("is_hypo").casefold() in ("1", "1.0", "true", "yes"),
                ec=get("event_confidence"), lc=get("location_confidence"), bc=get("boundary_confidence"),
                method=get("method", "geom_basis"), area=area, report_area=get("report_area_ha"),
                quotes=get("quotes", "quotations"), reasoning=get("reasoning"), uncertainty=get("uncertainty"),
                alternatives=get("alternatives", "alternative_hypotheses"))


def _round(coords):
    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (list, tuple)):
            return [_round(item) for item in coords]
        return [round(float(value), 6) for value in coords]
    return coords


def _features(gdf, props: list[dict]) -> list[dict]:
    """GeoJSON features in WGS84, simplified to about 1 m in the data's own projected CRS."""
    if gdf.crs is None:
        raise PipelineError("A placement layer has no coordinate reference system.")
    if gdf.crs.is_projected:
        gdf = gdf.assign(geometry=gdf.geometry.simplify(1.0, preserve_topology=True))
    gdf = gdf.to_crs(4326)
    features = []
    for geometry, prop in zip(gdf.geometry, props):
        if geometry is None or geometry.is_empty:
            continue
        shape = json.loads(json.dumps(geometry.__geo_interface__))
        shape["coordinates"] = _round(shape["coordinates"])
        features.append({"type": "Feature", "properties": prop, "geometry": shape})
    return features


def read_outputs(run_dir: Path, kind: str) -> dict:
    """A run's results and parts in the common form."""
    import geopandas as gpd

    outputs = run_dir / "localization" / "outputs"
    if kind == "agent":
        gdf = gpd.read_file(outputs / "placements.gpkg", layer="parts")
        results = {_clean(r.get("seg_id")): dict(status=_clean(r.get("status")), reason=_clean(r.get("reason")))
                   for r in _csv_rows(outputs / "segment_results.csv")}
    else:
        gdf = gpd.read_file(outputs / "segments.geojson")
        results = {_clean(r.get("seg_id")): dict(status=_clean(r.get("localization_status")), reason=_clean(r.get("reason")))
                   for r in _csv_rows(outputs / "segment_results.csv")} if (outputs / "segment_results.csv").is_file() else {}
    evidence_path = outputs / "segments_evidence.csv"
    evidence = {_clean(r.get("part_id")): r for r in _csv_rows(evidence_path)} if evidence_path.is_file() else {}
    columns = [c for c in gdf.columns if c != "geometry"]
    props = [_part({c: row[c] for c in columns}, evidence.get(_clean(row.get("part_id")))) for _, row in gdf.iterrows()]
    features = _features(gdf, props)
    for feature in features:  # a part without a results row still counts as located
        results.setdefault(feature["properties"]["seg"], dict(status="located", reason=""))
    return dict(results=results, features=features)


_CONTEXT_CACHE: dict[tuple, tuple] = {}


def context_layers(base_dir: Path) -> tuple[dict, list]:
    """Catchment, streams, roads and place names around the catchment, from the run's imported GIS data."""
    from segment_mapping.runctx import Run

    folder = base_dir / "localization"
    config_path = folder / "run.json"
    cache_key = (str(folder), config_path.stat().st_mtime)
    if cache_key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[cache_key]
    run = Run(folder)
    layers = ((run.config.get("package") or {}).get("layers")) or {}
    by_role: dict[str, list] = {}
    for name, info in layers.items():
        by_role.setdefault(info.get("role"), []).append((name, info))
    available = set(run.layer_names())

    def first(role, prefer=None):
        names = [item for item in by_role.get(role, []) if item[0] in available]
        names.sort(key=lambda item: item[0] != prefer)
        return names[0] if names else None

    out, bounds = {"catchment": [], "streams": [], "roads": [], "places": []}, None
    catchment = first("catchment")
    if catchment:
        catch = run.layer(catchment[0]).to_crs(run.crs)
        area = catch.union_all().buffer(2500)
        out["catchment"] = _features(catch[["geometry"]], [{"name": run.config.get("event", "")}] * len(catch))
        west, south, east, north = catch.to_crs(4326).total_bounds
        bounds = [[float(south), float(west)], [float(north), float(east)]]
        for layer_key, role, prefer in (("streams", "streams", "streams"), ("roads", "roads", "roads"), ("places", "names", "places")):
            item = first(role, prefer)
            if not item:
                continue
            g = run.layer(item[0]).to_crs(run.crs)
            g = g[g.intersects(area)].copy()
            if g.empty:
                continue
            g["geometry"] = g.geometry.intersection(area)
            g = g[~g.is_empty]
            field = item[1].get("name_field")
            names = [_clean(v) for v in g[field]] if field and field in g.columns else [""] * len(g)
            out[layer_key] = _features(g[["geometry"]], [{"name": n} for n in names])
    _CONTEXT_CACHE[cache_key] = (out, bounds)
    return out, bounds


def comparison_data(root: Path, run_ids: list[str]) -> dict:
    """Everything the compare page draws for the chosen runs of one report."""
    run_ids = list(dict.fromkeys(run_ids))
    if not run_ids:
        raise PipelineError("Choose at least one run to compare.")
    if len(run_ids) > MAX_COMPARED:
        raise PipelineError(f"Compare at most {MAX_COMPARED} runs at a time.")
    known = _entries(root)
    entries = []
    for run_id in run_ids:
        if run_id not in known:
            raise PipelineError(f"Run {run_id} was not found or has no localization outputs to compare.")
        entries.append(known[run_id])
    if len({entry["signature"] for entry in entries}) > 1:
        raise PipelineError("These runs localized different reports or segment tables; compare runs of one report only.")
    base_dir = root / entries[0]["base_run_id"]
    config = _localization_config(base_dir) or {}
    segments = [dict(id=_clean(s.get("seg_id")), process=_clean(s.get("process")), category=_clean(s.get("category")),
                     interaction=_clean(s.get("interact")), phase=_clean(s.get("phase"))) for s in config.get("segments") or []]
    context, bounds = context_layers(base_dir)
    models, runs = [], {}
    for index, entry in enumerate(entries):
        date = entry["created_at"][:16].replace("T", " ")
        models.append(dict(key=entry["run_id"], label=entry["model"], color=COLORS[index % len(COLORS)],
                           detail=f"{date} UTC · {entry['status'] or entry['kind']}"))
        runs[entry["run_id"]] = read_outputs(root / entry["run_id"], entry["kind"])
    return dict(models=models, reports=[dict(key=entries[0]["signature"], name=entries[0]["event"], segments=segments,
                                             context=context, bounds=bounds, runs=runs)])


# --------------------------------------------------------------------------- 3D terrain

TERRAIN_CELLS = 200  # grid nodes along the longer side of the terrain mesh
_TERRAIN_CACHE: dict[tuple, dict] = {}


def dem_file(base_dir: Path) -> Path | None:
    """The run's default elevation model (the DEM its placements used), if it is present."""
    config = _localization_config(base_dir) or {}
    name = config.get("default_dem") or "dem"
    rel = (config.get("rasters") or {}).get(name) or f"data_raw/{name}.tif"
    path = base_dir / "localization" / rel
    return path if path.is_file() else None


def _transform_coords(coords, transform):
    if isinstance(coords, (list, tuple)) and coords and isinstance(coords[0], (list, tuple)):
        return [_transform_coords(item, transform) for item in coords]
    x, y = transform(coords[0], coords[1])
    return [round(float(x), 1), round(float(y), 1)]


def _terrain_grid(base_dir: Path, source="run", upload=None, fallback_bounds=None, extent="catchment") -> dict:
    """A down-sampled DEM around the catchment, in the run's projected CRS, rows from north to south."""
    import numpy as np
    import rasterio
    from pyproj import CRS, Transformer
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds
    from rasterio.vrt import WarpedVRT
    from rasterio.transform import from_bounds

    from segment_mapping.runctx import Run

    from .terrain_sources import terrain_dem

    run = Run(base_dir / "localization")
    crs = CRS.from_user_input(run.crs)
    if not crs.is_projected:
        raise PipelineError("3D viewing requires a projected run coordinate system in metres.")
    layers = ((run.config.get("package") or {}).get("layers")) or {}
    catchment = next((name for name, info in layers.items() if info.get("role") == "catchment" and name in run.layer_names()), None)
    bounds = None
    if catchment:
        x0, y0, x1, y1 = run.layer(catchment).to_crs(crs).total_bounds
        pad = 0.15 * max(x1 - x0, y1 - y0) + 300
        bounds = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    elif fallback_bounds:
        bounds = transform_bounds(4326, crs, *fallback_bounds)
        pad = .15 * max(bounds[2] - bounds[0], bounds[3] - bounds[1]) + 300
        bounds = (bounds[0]-pad, bounds[1]-pad, bounds[2]+pad, bounds[3]+pad)
    dem, credit = terrain_dem(base_dir, source, bounds, crs, upload)
    key = (str(dem), dem.stat().st_mtime_ns, str(crs), bounds, extent, source)
    if key in _TERRAIN_CACHE:
        return _TERRAIN_CACHE[key]
    with rasterio.open(dem) as ds:
        if not ds.crs:
            raise PipelineError("The DEM has no coordinate system. Upload a GeoTIFF with a defined CRS.")
        b = transform_bounds(ds.crs, crs, *ds.bounds)
        requested_bounds = bounds
        bounds = b if extent == "full" else (bounds or b)
        bounds = (max(bounds[0], b[0]), max(bounds[1], b[1]), min(bounds[2], b[2]), min(bounds[3], b[3]))
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        if width <= 0 or height <= 0:
            raise PipelineError("The DEM does not overlap this report's mapped area.")
        step = max(width, height) / (TERRAIN_CELLS - 1)
        nx, ny = max(2, round(width / step) + 1), max(2, round(height / step) + 1)
        # Mesh nodes sit on the bounds; raster samples sit at pixel centres.
        dx, dy = width / (nx - 1), height / (ny - 1)
        transform = from_bounds(bounds[0]-dx/2, bounds[1]-dy/2, bounds[2]+dx/2, bounds[3]+dy/2, nx, ny)
        with WarpedVRT(ds, crs=crs, transform=transform, width=nx, height=ny,
                       resampling=Resampling.bilinear, dtype="float32", nodata=float("nan")) as vrt:
            z = vrt.read(1, masked=True).astype("float64")
    z = np.ma.masked_invalid(z)
    if z.count() == 0:
        raise PipelineError("The elevation model has no data around the catchment.")
    filled = z.filled(float(z.min()))
    # Affine (lon, lat) -> (x, y) fitted on a grid of points: exact enough over a catchment to place basemap tiles.
    to_wgs = Transformer.from_crs(crs, 4326, always_xy=True)
    xs, ys = np.meshgrid(np.linspace(bounds[0], bounds[2], 5), np.linspace(bounds[1], bounds[3], 5))
    lon, lat = to_wgs.transform(xs.ravel(), ys.ravel())
    design = np.column_stack([lon, lat, np.ones_like(lon)])
    ax, *_ = np.linalg.lstsq(design, xs.ravel(), rcond=None)
    ay, *_ = np.linalg.lstsq(design, ys.ravel(), rcond=None)
    result = dict(crs=crs.to_string(), bounds=[round(v, 1) for v in bounds], nx=nx, ny=ny,
                  z=[round(float(v), 1) for v in filled.ravel()], zmin=round(float(filled.min()), 1),
                  zmax=round(float(filled.max()), 1), lonlat_to_xy=[float(v) for v in (*ax, *ay)],
                  lonlat_bounds=[float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())],
                  dem=dem.name, attribution=credit, source=source, extent=extent,
                  notice=("This elevation model has finite coverage. Terrain outside the file is unavailable."
                          if extent == "full" else
                          "The elevation file covers only part of the requested surroundings."
                          if requested_bounds and any((b[0] > requested_bounds[0], b[1] > requested_bounds[1],
                                                       b[2] < requested_bounds[2], b[3] < requested_bounds[3])) else ""))
    if source == "copernicus" and extent == "full":
        result["notice"] = "Showing the downloaded Copernicus region. For continuous worldwide exploration, select Mapzen with Full available terrain."
    if np.ma.getmaskarray(z).any():
        result["notice"] += " Missing elevation cells are shown at the lowest available height; these areas are not measured terrain."
    _TERRAIN_CACHE[key] = result
    return result


def terrain_data(root: Path, run_ids: list[str], source="run", upload=None, extent="catchment") -> dict:
    """Terrain, context and each run's parts in the run's projected CRS, for the 3D view."""
    from pyproj import Transformer

    if extent not in ("catchment", "full"):
        raise PipelineError("Choose catchment surroundings or full available terrain.")
    data = comparison_data(root, run_ids)
    report = data["reports"][0]
    if extent == "full" and source == "online":
        from .terrain_sources import MAPZEN_CREDIT
        return dict(models=data["models"], name=report["name"], segments=report["segments"],
                    terrain=dict(source=source, extent=extent, streaming=True, attribution=MAPZEN_CREDIT,
                                 notice="Global elevation streams as you pan and zoom. Detail varies by region; coverage ends at 85.05 degrees latitude. Left-drag to rotate and tilt; right-drag to pan; scroll to zoom."),
                    bounds=report["bounds"], context=report["context"], runs=report["runs"])
    base_dir = root / _entries(root)[data["models"][0]["key"]]["base_run_id"]
    features = report["context"].get("catchment", []) or [f for r in report["runs"].values() for f in r["features"]]
    from shapely.geometry import shape
    boxes = [shape(f["geometry"]).bounds for f in features if f.get("geometry") and not shape(f["geometry"]).is_empty]
    bounds = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None
    grid = _terrain_grid(base_dir, source, upload, bounds, extent)
    to_xy = Transformer.from_crs(4326, grid["crs"], always_xy=True).transform

    def local(features):
        return [{"type": "Feature", "properties": f["properties"],
                 "geometry": {"type": f["geometry"]["type"], "coordinates": _transform_coords(f["geometry"]["coordinates"], to_xy)}}
                for f in features]

    context = {key: local(value) for key, value in report["context"].items() if key in ("catchment", "streams", "roads")}
    runs = {key: dict(results=run["results"], features=local(run["features"])) for key, run in report["runs"].items()}
    return dict(models=data["models"], name=report["name"], segments=report["segments"], terrain=grid,
                context=context, runs=runs)


# --------------------------------------------------------------------------- importing and exporting runs

BUNDLE_FORMAT = "localization-runs/1"
BUNDLE_MAX_BYTES = 2 * 1024**3
# Files that make a pipeline run comparable elsewhere: its manifest, segments and GIS context, and its outputs.
PIPELINE_FILES = ("manifest.json", "localization/run.json", "localization/data_raw/sources.gpkg",
                  "localization/llm/checkpoint.json")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(text: str) -> str:
    return new_run_id(text).rsplit("__", 1)[0]


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PipelineError("The upload is not a valid zip file.") from exc
    if sum(info.file_size for info in archive.infolist()) > BUNDLE_MAX_BYTES:
        raise PipelineError("The zip unpacks to more than 2 GB.")
    return archive


def _members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Zip members by normalized path; paths that could leave the target folder are refused."""
    out = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        parts = [part for part in name.split("/") if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts) or ":" in parts[0] or name.startswith("/"):
            raise PipelineError(f"The zip contains an unsafe path: {info.filename}")
        if parts[0] == "__MACOSX":
            continue
        out["/".join(parts)] = info
    return out


def _check_agent_outputs(folder: Path, segment_ids: list[str]) -> None:
    import geopandas as gpd

    try:
        gdf = gpd.read_file(folder / "placements.gpkg", layer="parts")
    except Exception as exc:  # noqa: BLE001 - any read failure means the file is unusable
        raise PipelineError(f"placements.gpkg has no readable layer 'parts': {exc}") from exc
    if gdf.crs is None:
        raise PipelineError("placements.gpkg: the 'parts' layer has no coordinate reference system.")
    for column in ("seg_id", "part_id"):
        if column not in gdf.columns:
            raise PipelineError(f"placements.gpkg: the 'parts' layer needs a '{column}' field.")
    results = _csv_rows(folder / "segment_results.csv")
    if not results or not {"seg_id", "status"} <= set(results[0]):
        raise PipelineError("segment_results.csv needs the columns seg_id and status.")
    known = set(segment_ids)
    listed = {_clean(r["seg_id"]) for r in results}
    mapped = {_clean(v) for v in gdf["seg_id"]}
    unknown = sorted((listed | mapped) - known, key=str)
    if unknown:
        raise PipelineError("These segments are not in the chosen report's segment table: " + ", ".join(unknown))
    absent = sorted(known - listed, key=str)
    if absent:
        raise PipelineError("segment_results.csv has no row for segments " + ", ".join(absent))


def _store_agent(root: Path, *, report: str, model: str, files: dict[str, bytes], source: str,
                 created_at: str | None = None) -> tuple[str, Path]:
    """Save one agent run of a report. Returns ("imported" or "skipped", run folder)."""
    model = " ".join(str(model or "").split())
    if not model:
        raise PipelineError("Enter the model (and settings) that produced this run.")
    missing = [name for name, required in AGENT_FILES.items() if required and name not in files]
    if missing:
        raise PipelineError("The run is missing " + ", ".join(missing) + ".")
    group = next((g for g in comparable_groups(root) if g["signature"] == report), None)
    if group is None:
        raise PipelineError("Choose the report this agent localized (no localization run of it is here yet).")
    digest = _sha(files["placements.gpkg"])
    for run in group["runs"]:
        if run["kind"] != "agent":
            continue
        agent = read_json(root / run["run_id"] / "manifest.json").get("agent") or {}
        if agent.get("model") == model and agent.get("placements_sha256") == digest:
            return "skipped", root / run["run_id"]
    base_run_id = group["base_run_id"]
    run_id = new_run_id(f"{group['event']} {model}")
    run_dir = root / run_id
    outputs = run_dir / "localization" / "outputs"
    try:
        outputs.mkdir(parents=True)
        for name, content in files.items():
            (outputs / name).write_bytes(content)
        config = _localization_config(root / base_run_id) or {}
        _check_agent_outputs(outputs, [_clean(s.get("seg_id")) for s in config.get("segments") or []])
        manifest = dict(run_id=run_id, kind=AGENT_KIND, event=group["event"], status="imported",
                        created_at=created_at or utc_now(),
                        agent=dict(model=model, signature=report, base_run_id=base_run_id, source=source,
                                   imported_at=utc_now(), files=sorted(files), placements_sha256=digest))
        save_manifest(run_dir, manifest)
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return "imported", run_dir


def import_agent_run(root: Path, *, report: str, model: str, data: bytes, filename: str) -> Path:
    """Import one agent run from a zip of its outputs (folders inside the zip are ignored)."""
    archive = _open_zip(data)
    files: dict[str, bytes] = {}
    for name, info in _members(archive).items():
        base = name.rsplit("/", 1)[-1]
        if base in AGENT_FILES:
            if base in files:
                raise PipelineError(f"The zip contains more than one {base}; import several runs as a bundle.")
            files[base] = archive.read(info)
    _, run_dir = _store_agent(root, report=report, model=model, files=files, source=Path(filename or "upload.zip").name)
    return run_dir


def _store_pipeline(root: Path, entry: dict, members: dict, archive: zipfile.ZipFile, prefix: str) -> tuple[str, Path]:
    run_id = str(entry.get("run_id", ""))
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise PipelineError("A pipeline run in the bundle has no valid run_id.")
    run_dir = root / run_id
    if (run_dir / "manifest.json").exists():
        return "skipped", run_dir
    outputs = "localization/outputs/"
    wanted = {}
    for name, info in members.items():
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        raw = "localization/data_raw/"
        dem = rel.startswith(raw) and "/" not in rel[len(raw):] and rel.endswith(".tif")
        if rel in PIPELINE_FILES or dem or (rel.startswith(outputs) and "/" not in rel[len(outputs):]):
            wanted[rel] = info
    for required in ("manifest.json", "localization/run.json", "localization/outputs/segments.geojson"):
        if required not in wanted:
            raise PipelineError(f"Pipeline run {run_id} is missing {required}.")
    try:
        for rel, info in wanted.items():
            target = run_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
        manifest = read_json(run_dir / "manifest.json")
        if manifest.get("run_id") not in (None, run_id):
            raise PipelineError(f"Pipeline run {run_id}: its manifest names another run.")
        if _pipeline_entry(run_dir, manifest) is None:
            raise PipelineError(f"Pipeline run {run_id} has no readable localization outputs.")
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return "imported", run_dir


def import_runs(root: Path, data: bytes, *, filename: str, report: str | None = None, model: str | None = None) -> list[dict]:
    """Import every run in a zip: an exported bundle (with bundle.json), or one or more folders of agent outputs.

    A bundle names each run's report and model and carries the pipeline runs that give the report's segments and
    GIS context, so it imports into an empty workspace. Plain agent folders are attached to the chosen report;
    their model is the entered model for a single run, otherwise the folder name. Runs already present are skipped.
    """
    archive = _open_zip(data)
    members = _members(archive)
    source = Path(filename or "upload.zip").name
    bundles = sorted((name for name in members if name.rsplit("/", 1)[-1] == "bundle.json"), key=lambda n: n.count("/"))
    outcomes: list[dict] = []

    def attempt(label, kind, work):
        try:
            status, run_dir = work()
            outcomes.append(dict(label=label, kind=kind, status=status, run_id=run_dir.name, detail=""))
        except PipelineError as exc:
            outcomes.append(dict(label=label, kind=kind, status="failed", run_id="", detail=str(exc)))

    if bundles:
        prefix = bundles[0][: -len("bundle.json")]
        try:
            bundle = json.loads(archive.read(members[bundles[0]]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PipelineError(f"bundle.json is not valid JSON: {exc}") from exc
        if bundle.get("format") != BUNDLE_FORMAT:
            raise PipelineError(f"Unknown bundle format {bundle.get('format')!r}; expected {BUNDLE_FORMAT}.")
        for entry in sorted(bundle.get("runs") or [], key=lambda e: e.get("kind") != "pipeline"):
            # Pipeline runs first: agent runs attach to the report they provide.
            folder = prefix + str(entry.get("path", "")).strip("/") + "/"
            kind = "pipeline" if entry.get("kind") == "pipeline" else "agent"
            label = f"{entry.get('event', '')} · {entry.get('model', '')}"
            if kind == "pipeline":
                attempt(label, kind, lambda entry=entry, folder=folder: _store_pipeline(root, entry, members, archive, folder))
            else:
                files = {name[len(folder):]: archive.read(info) for name, info in members.items()
                         if name.startswith(folder) and name[len(folder):] in AGENT_FILES}
                attempt(label, kind, lambda entry=entry, files=files: _store_agent(
                    root, report=str(entry.get("signature", "")), model=entry.get("model", ""), files=files,
                    source=source, created_at=entry.get("created_at")))
        return outcomes

    folders: dict[str, dict[str, bytes]] = {}
    for name, info in members.items():
        folder, _, base = name.rpartition("/")
        if base in AGENT_FILES:
            folders.setdefault(folder, {})[base] = archive.read(info)
    folders = {folder: files for folder, files in folders.items() if "placements.gpkg" in files}
    if not folders:
        raise PipelineError("The zip contains no run: each run needs a folder with placements.gpkg, segment_results.csv "
                            "and segments_evidence.csv, or the zip must be a bundle downloaded from Compare.")
    if not report:
        raise PipelineError("Choose the report these runs localized.")
    single = len(folders) == 1
    for folder, files in sorted(folders.items()):
        name = model if single and model else ([part for part in folder.split("/") if part != "outputs"] or [Path(source).stem])[-1]
        attempt(name, "agent", lambda name=name, files=files: _store_agent(root, report=report, model=name, files=files,
                                                                           source=source))
    return outcomes


def export_runs(root: Path, run_ids: list[str]) -> tuple[bytes, str]:
    """A bundle zip of runs (pipeline and agent) that import_runs loads in another workspace."""
    known = _entries(root)
    entries = [known[run_id] for run_id in dict.fromkeys(run_ids) if run_id in known]
    if not entries:
        raise PipelineError("Choose at least one run to download.")
    buffer = io.BytesIO()
    listed = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            run_dir = root / entry["run_id"]
            path = f"{_slug(entry['event'] or 'report')}/{entry['kind']}-{entry['run_id']}"
            item = dict(path=path, kind=entry["kind"], run_id=entry["run_id"], event=entry["event"],
                        signature=entry["signature"], segments=entry["segments"], created_at=entry["created_at"])
            outputs = run_dir / "localization" / "outputs"
            if entry["kind"] == "pipeline":
                item["model"] = (_localization_config(run_dir) or {}).get("model") or ""
                names = [rel for rel in PIPELINE_FILES if (run_dir / rel).is_file()]
                dem = dem_file(run_dir)
                if dem is not None:
                    names.append(dem.relative_to(run_dir).as_posix())
                names += [f"localization/outputs/{p.name}" for p in sorted(outputs.iterdir())
                          if p.is_file() and not p.name.endswith("_review.zip")]
                for rel in names:
                    archive.write(run_dir / rel, f"{path}/{rel}")
            else:
                item["model"] = entry["model"]
                for p in sorted(outputs.iterdir()):
                    if p.is_file() and p.name in AGENT_FILES:
                        archive.write(p, f"{path}/{p.name}")
            listed.append(item)
        events = sorted({entry["event"] for entry in entries})
        archive.writestr("bundle.json", json.dumps(dict(format=BUNDLE_FORMAT, created_at=utc_now(), runs=listed),
                                                   ensure_ascii=False, indent=1))
        archive.writestr("README.txt", (
            "Localization runs for " + ", ".join(events) + "\n\n"
            "To compare them side by side, import this zip unchanged in the Multi-hazard Review interface:\n"
            "Compare -> Import runs. Each folder is one run. Pipeline runs hold the report's segments\n"
            "(localization/run.json), its GIS context (localization/data_raw/sources.gpkg) and their outputs; agent runs\n"
            "hold placements.gpkg, segment_results.csv, segments_evidence.csv and the agent's report.md.\n"
            "bundle.json lists every run with its report and model.\n"))
    name = "_".join(_slug(event) for event in events) or "localization"
    return buffer.getvalue(), f"{name}_runs_{utc_now()[:10]}.zip"
