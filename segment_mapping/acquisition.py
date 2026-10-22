"""Bounded, opt-in acquisition for localizer tool calls.

Parent integration: expose REQUEST_SCHEMA as the model tool schema, then call
acquire_source(run, arguments, package=loaded_package, allow_network=cli_flag).
Return its result to the model; refresh Run after successful acquisition. Package
``acquisition.providers`` contains trusted source declarations (id, role and the
same item fields as sources presets). A provider can instead reference an installed
preset using ``preset`` and ``item``. Model arguments cannot supply URLs or SQL.
Core inputs remain mandatory: acquisition never manufactures a partial package.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from . import sources
from .package import REQUIRED_ROLES, TIROL_FIELDS, _dem_coverage, _metre_crs
from .runctx import Run, read_json, today, write_json

REQUEST_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"role": {"type": "string", "minLength": 1, "maxLength": 64},
                   "query": {"type": "string", "minLength": 1, "maxLength": 200},
                   "provider": {"type": "string", "minLength": 1, "maxLength": 100}},
    "required": ["role", "query"],
}
_LOCK = threading.RLock()


def validate_request(request):
    if not isinstance(request, dict) or set(request) - {"role", "query", "provider"} or not {"role", "query"} <= set(request):
        raise ValueError("request requires role and query; only optional provider is allowed")
    for key, value in request.items():
        limit = {"role": 64, "query": 200, "provider": 100}[key]
        if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError(f"invalid {key}")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", request["role"]):
        raise ValueError("role must be a lower-case role identifier")
    return {k: v.strip() for k, v in request.items()}


def acquisition_providers(package=None, *, preset=None):
    """List trusted declarations. Regional presets must be explicitly selected by caller."""
    declared = (package or {}).get("acquisition", {}).get("providers", [])
    result = []
    if preset:
        p = sources.load_preset(preset)
        for item in p.get("items", []):
            # Do not implicitly offer OSM or imagery as official geometry sources.
            if item.get("role") and item.get("type") in {"arcgis", "wcs", "file"}:
                result.append({**{k: p[k] for k in ("provider", "licence", "licence_url") if k in p},
                               **item, "id": f"{preset}:{item['name']}",
                               **({k: v for k, v in TIROL_FIELDS.get(item["name"], {}).items() if k != "role"} if preset == "tirol" else {}),
                               **({"query_field": TIROL_FIELDS[item["name"]]["name_field"]}
                                  if preset == "tirol" and TIROL_FIELDS.get(item["name"], {}).get("name_field") else {})})
    for declaration in declared:
        if "preset" in declaration:
            candidates = acquisition_providers(preset=declaration["preset"])
            match = next((x for x in candidates if x["name"] == declaration.get("item")), None)
            if match is None:
                raise ValueError(f"unknown declared preset item: {declaration}")
            result.append({**match, "id": declaration.get("id", match["id"])})
        else:
            result.append(dict(declaration))
    ids = [p.get("id") for p in result]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("acquisition provider IDs must be present and unique")
    return result


def _verify_raster_dimensions(ds, max_raster_pixels, target_crs=None):
    if ds.width * ds.height > max_raster_pixels:
        raise ValueError("raster exceeds pixel limit")
    if target_crs and ds.crs and ds.crs != target_crs:
        from rasterio.warp import calculate_default_transform
        _, width, height = calculate_default_transform(
            ds.crs, target_crs, ds.width, ds.height, *ds.bounds, resolution=abs(ds.res[0]))
        if width * height > max_raster_pixels:
            raise ValueError("reprojected raster exceeds pixel limit")


def _verify_file(path, layer=None, *, max_raster_pixels, target_crs=None):
    """Inspect original metadata before existing fetchers transform any data."""
    if Path(path).suffix.lower() in {".tif", ".tiff"}:
        import rasterio
        with rasterio.open(path) as ds:
            _verify_raster_dimensions(ds, max_raster_pixels, target_crs)
            if ds.crs is None:
                raise ValueError("downloaded raster has no CRS")
            import numpy as np
            if not any(np.any(np.isfinite(band.compressed())) for _, window in ds.block_windows(1)
                       for band in [ds.read(1, window=window, masked=True)]):
                raise ValueError("downloaded raster has no valid finite cells")
            return ds.crs.to_string()
    import geopandas as gpd
    g = gpd.read_file(path, **({"layer": layer} if layer else {}))
    if g.crs is None:
        raise ValueError("downloaded vector has no CRS")
    if g.empty or g.geometry.isna().any() or g.geometry.is_empty.any() or not g.geometry.is_valid.all():
        raise ValueError("downloaded vector is empty or has invalid/missing geometries")
    return g.crs.to_string()


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def acquire_source(run: Run, request: dict, *, package=None, preset=None, allow_network=False,
                   max_http_calls=32, retries=2, max_area_m2=2_500_000_000, max_raster_pixels=16_000_000,
                   max_download_bytes=128 * 1024 * 1024, max_elapsed_seconds=180):
    """Execute one declared request; return and persist success/failure, provenance and gaps.

    No geocoding guess is made: caller must configure a bounded run bbox. query
    filters a declared query_field (literal substring) or is recorded as place
    context for spatial-only sources. Each HTTP attempt is recorded, including
    retries; cached success is reused only while its verified output still exists.
    Calls serialize the legacy fetcher HTTP adapter to keep telemetry and CRS
    verification attached to the correct request. The download byte budget is
    cumulative across responses and retries; redirecting providers must declare
    their final HTTPS URL. Raster headers are checked before reading cell arrays.
    """
    request = validate_request(request)
    if not 1 <= retries <= 3 or not 1 <= max_http_calls <= 64:
        raise ValueError("retries must be 1..3 and max_http_calls 1..64")
    if (not math.isfinite(max_elapsed_seconds) or max_elapsed_seconds <= 0
            or not math.isfinite(max_download_bytes) or max_download_bytes <= 0
            or not math.isfinite(max_raster_pixels) or max_raster_pixels <= 0):
        raise ValueError("acquisition time, download and raster budgets must be positive and finite")
    deadline = time.monotonic() + max_elapsed_seconds
    result = dict(request=request, status="failed", accessed=today(), cache_hit=False,
                  attempts=[], data_gaps=[], network_allowed=bool(allow_network))
    key = None
    cache_path = None
    with _LOCK:
        try:
            providers = acquisition_providers(package, preset=preset)
            matches = [p for p in providers if p.get("role") == request["role"]
                       and (not request.get("provider") or p["id"] == request["provider"])]
            if not matches:
                raise ValueError(f"no declared provider for role {request['role']}")
            if len(matches) > 1:
                raise ValueError(f"select provider explicitly: {[p['id'] for p in matches]}")
            p = matches[0]
            result["provider"] = p
            _metre_crs(run.crs)
            bbox = run.bbox()
            if not bbox or len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
                raise ValueError("a finite caller-configured bbox in run CRS is required")
            width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if min(width, height) <= 0 or width * height > max_area_m2:
                raise ValueError("acquisition bbox is empty or exceeds area limit")
            result["scope"] = dict(bbox=list(bbox), crs=run.crs)
            name = p.get("name") or p["id"]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
                raise ValueError("provider name must be a safe dataset identifier")
            kind = p.get("type")
            if kind not in {"arcgis", "wcs", "file"}:
                raise ValueError(f"unsupported acquisition provider type {kind!r}; use arcgis, wcs or file")
            if kind == "wcs":
                res = float(p["res"])
                if (not math.isfinite(res) or res <= 0
                        or ((math.ceil(bbox[2] / res) - math.floor(bbox[0] / res))
                            * (math.ceil(bbox[3] / res) - math.floor(bbox[1] / res))) > max_raster_pixels):
                    raise ValueError("raster request exceeds pixel limit or has invalid resolution")
            query_field = p.get("query_field")
            if query_field and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", query_field):
                raise ValueError("invalid declared query_field")
            literal = request["query"].replace("'", "''").replace("%", "").replace("_", " ")
            where = f"UPPER({query_field}) LIKE '%{literal.upper()}%'" if query_field and kind == "arcgis" else "1=1"
            result["query_application"] = dict(mode="attribute_substring" if where != "1=1" else "spatial_scope_only", where=where)
            key = hashlib.sha256(json.dumps([request, p, bbox, run.crs], sort_keys=True).encode()).hexdigest()
            # Distinct requests never overwrite supplied inputs or previously verified query results.
            name = f"acq_{name[:55]}_{key[:8]}"
            cache_path = run.path(f"data_raw/acquisition_cache/{key}.json")
            cached = read_json(cache_path)
            if cached and cached.get("status") == "success":
                rec = cached["source"]
                try:
                    target = run.path(rec["file"]) if rec.get("file") else run.gpkg
                    _verify_file(target, None if rec.get("file") else name, max_raster_pixels=max_raster_pixels)
                    if _digest(target) != cached.get("output_sha256"):
                        raise ValueError("cached dataset changed")
                except Exception:
                    pass
                else:
                    result.update(cached, cache_hit=True, attempts=[], network_allowed=bool(allow_network))
                    return result
            url = p.get("service") if kind == "arcgis" else p.get("url")
            local = kind == "file" and bool(url) and Path(url).is_file()
            if not local and not allow_network:
                raise ValueError("network acquisition disabled by caller")
            if not local and (not url or urlparse(url).scheme != "https"):
                raise ValueError("declared remote provider must use HTTPS")
            common = {k: p.get(k, "") for k in ("provider", "licence", "licence_url", "use")}
            original_http = sources.http
            downloaded_bytes = 0
            wcs_headers = []

            def buffer_response(response):
                """Bound decoded bytes across every response and partial retry body."""
                nonlocal downloaded_bytes
                body = bytearray()
                try:
                    for chunk in response.iter_content(chunk_size=int(min(65536, max_download_bytes + 1))):
                        downloaded_bytes += len(chunk)
                        result["download_bytes"] = downloaded_bytes
                        if downloaded_bytes > max_download_bytes:
                            raise ValueError("source download exceeds byte limit")
                        if time.monotonic() > deadline:
                            raise ValueError("download exceeded elapsed-time budget")
                        body.extend(chunk)
                    if time.monotonic() > deadline:
                        raise ValueError("download exceeded elapsed-time budget")
                    # Keep the requests.Response interface used by the legacy fetchers.
                    response._content = bytes(body)
                    response._content_consumed = True
                finally:
                    response.close()
                    if response.raw is not None:
                        response.raw.close()

            def audited_http(method, endpoint, **kwargs):
                kwargs.pop("retries", None)
                kwargs["stream"] = True
                # requests otherwise drains redirect bodies outside our byte budget.
                kwargs["allow_redirects"] = False
                for attempt in range(1, retries + 1):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("acquisition elapsed-time budget exhausted")
                    kwargs["timeout"] = min(float(kwargs.get("timeout", 30)), remaining, 30)
                    if len(result["attempts"]) >= max_http_calls:
                        raise ValueError("acquisition HTTP request budget exhausted")
                    trace = dict(method=method, url=endpoint, attempt=attempt,
                                 query=kwargs.get("params", kwargs.get("data")))
                    result["attempts"].append(trace)
                    try:
                        response = original_http(method, endpoint, retries=1, **kwargs)
                        trace.update(status_code=response.status_code, status="received")
                        buffer_response(response)
                        if 300 <= response.status_code < 400:
                            raise ValueError("acquisition redirects are unsupported; declare the final HTTPS URL")
                        if kind == "arcgis" and endpoint.rstrip("/").endswith("/query"):
                            from pyproj import CRS
                            payload = response.json()
                            if payload.get("features"):
                                reference = payload.get("spatialReference") or {}
                                actual = reference.get("latestWkid") or reference.get("wkid")
                                if actual is None or CRS.from_epsg(actual) != CRS(run.crs):
                                    raise ValueError(f"ArcGIS returned CRS {reference}; expected {run.crs}; refusing relabel")
                                trace["actual_crs"] = f"EPSG:{actual}"
                        if kind == "wcs" and response.content[:4] in (b"II*\x00", b"MM\x00*"):
                            from rasterio.io import MemoryFile
                            from pyproj import CRS
                            with MemoryFile(response.content) as memory, memory.open() as ds:
                                _verify_raster_dimensions(ds, max_raster_pixels)
                                if ds.crs is None or CRS(ds.crs) != CRS(run.crs):
                                    raise ValueError(f"WCS returned CRS {ds.crs}; expected {run.crs}; refusing relabel")
                                trace["actual_crs"] = ds.crs.to_string()
                                # Legacy merge allocates the union at the first tile's resolution.
                                wcs_headers.append((ds.bounds, ds.res, ds.width * ds.height))
                                bounds = [header[0] for header in wcs_headers]
                                rx, ry = wcs_headers[0][1]
                                mosaic_width = max(1, round((max(b.right for b in bounds) - min(b.left for b in bounds)) / rx))
                                mosaic_height = max(1, round((max(b.top for b in bounds) - min(b.bottom for b in bounds)) / ry))
                                if (sum(header[2] for header in wcs_headers) > max_raster_pixels
                                        or mosaic_width * mosaic_height > max_raster_pixels):
                                    raise ValueError("WCS mosaic exceeds pixel limit")
                        return response
                    except Exception as exc:
                        # HTTP failures can carry a streamed error body, too.
                        error_response = getattr(exc, "response", None)
                        if error_response is not None:
                            try:
                                buffer_response(error_response)
                            except Exception as budget_exc:
                                trace.update(status="failed", error=str(budget_exc))
                                raise
                        trace.update(status="failed", error=str(exc))
                        if attempt == retries or isinstance(exc, ValueError):
                            raise

            sources.http = audited_http
            try:
                if kind == "arcgis":
                    rec = sources.fetch_arcgis(run, name, url, p.get("layer_name"), p.get("layer_id"), bbox=bbox, where=where, **common)
                elif kind == "wcs":
                    rec = sources.fetch_wcs(run, name, url, p["coverage"], res, bbox, p.get("version", "1.0.0"), **common)
                else:
                    suffix = Path(urlparse(url).path).suffix.lower()
                    if suffix not in {".gpkg", ".geojson", ".tif", ".tiff"}:
                        raise ValueError("file acquisition supports gpkg, geojson and geotiff; archives/virtual datasets are unsupported")
                    source_path = Path(url) if local else run.path(f"data_raw/acquisition_cache/{key}{suffix}")
                    if not local:
                        response = audited_http("GET", url, timeout=120, stream=True)
                        source_path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            with source_path.open("wb") as destination:
                                for chunk in response.iter_content(1024 * 1024):
                                    if time.monotonic() > deadline:
                                        raise ValueError("download exceeded elapsed-time budget")
                                    destination.write(chunk)
                        finally:
                            response.close()
                    result["downloaded_crs"] = _verify_file(source_path, p.get("layer"), max_raster_pixels=max_raster_pixels, target_crs=run.crs)
                    rec = sources.fetch_file(run, name, str(source_path), p.get("layer"), bbox=bbox, **common)
                    rec["url"] = url
            finally:
                sources.http = original_http
            if rec.get("kind") == "vector":
                if not rec.get("count"):
                    raise ValueError("source query returned zero features")
                _verify_file(run.gpkg, name, max_raster_pixels=max_raster_pixels)
            elif rec.get("file"):
                _verify_file(run.path(rec["file"]), max_raster_pixels=max_raster_pixels)
                if request["role"] == "dem":
                    from shapely.ops import unary_union
                    known = (run.config.get("package") or {}).get("layers", {})
                    cats = [run.layer(n).geometry.union_all() for n, info in known.items() if info.get("role") == "catchment"]
                    if cats:
                        result["dem_coverage"] = _dem_coverage(run.path(rec["file"]), unary_union(cats), run.crs)
                        if result["dem_coverage"]["coverage_fraction"] < .99:
                            raise ValueError("acquired DEM covers less than 99% of the catchment; see dem_coverage gaps")
                    else:
                        result["data_gaps"].append(dict(role="catchment", reason="DEM catchment coverage unverified: no catchment available"))
            else:
                raise ValueError("fetcher returned no usable output")
            rec.update(role=request["role"], acquisition_request=request)
            run.record_source(rec)
            fresh = Run(run.dir)
            fresh.config.setdefault("package", {}).setdefault("layers", {})[name] = {
                "role": request["role"], **{k: p[k] for k in ("name_field", "id_field", "ref_field", "value_field") if k in p}}
            fresh.save_config()
            run.config.update(fresh.config)
            run._layers.pop(name, None)
            result.update(status="success", source=rec, output_name=name)
            result["output_sha256"] = _digest(run.path(rec["file"]) if rec.get("file") else run.gpkg)
            roles = {info.get("role") for info in fresh.config["package"]["layers"].values()}
            result["missing_core_roles"] = [r for r in REQUIRED_ROLES if r not in roles]
            result["requires_preflight"] = True
            write_json(cache_path, result)
        except (Exception, SystemExit) as exc:
            result["error"] = str(exc)
            result["data_gaps"].append(dict(role=request["role"], query=request["query"], reason=str(exc)))
        finally:
            history = run.path("data_raw/acquisition_history.json")
            records = read_json(history, [])
            records.append(result)
            write_json(history, records)
    return result
