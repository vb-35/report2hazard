"""Data acquisition from public services. Region-independent fetchers plus JSON presets.

Every fetcher writes ONE projected copy (run CRS) of its dataset into data_raw and a
manifest record (name, provider, URL, layer, date/version, licence, request, count).
"""
from __future__ import annotations

import io
import json
import math
import re
import tempfile
import time
import zipfile
from pathlib import Path

import numpy as np
import requests

from .runctx import Run, today

UA = {"User-Agent": "segment-mapping-toolkit/1.0 (research use; event report GIS placement)"}
PRESET_DIR = Path(__file__).with_name("presets")
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]


def http(method: str, url: str, retries: int = 3, timeout: float = 120, **kw) -> requests.Response:
    last = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, headers={**UA, **kw.pop("headers", {})}, timeout=timeout, **kw)
            if r.status_code in (429, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} from {url}", response=r)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


def _bbox_wgs84(run: Run, bbox):
    from pyproj import Transformer

    t = Transformer.from_crs(run.crs, 4326, always_xy=True)
    xs, ys = t.transform([bbox[0], bbox[2], bbox[0], bbox[2]], [bbox[1], bbox[1], bbox[3], bbox[3]])
    return min(xs), min(ys), max(xs), max(ys)


def bbox_for(run: Run, which: str = "bbox"):
    b = run.bbox("detail_bbox" if which == "detail" else "bbox") or run.bbox("bbox")
    if not b:
        raise SystemExit("No bbox configured. Use `config RUN --bbox x0,y0,x1,y1` (run CRS) or `--bbox-wgs84`.")
    return b


def load_preset(name: str) -> dict:
    p = PRESET_DIR / f"{name}.json"
    if not p.exists():
        raise SystemExit(f"Unknown preset {name!r}. Available: {[q.stem for q in PRESET_DIR.glob('*.json')]}")
    return json.loads(p.read_text(encoding="utf-8"))


# ============================================================================ ArcGIS REST


def _esri_geom(g: dict):
    from shapely.geometry import LineString, MultiLineString, MultiPoint, Point, Polygon
    from shapely.ops import unary_union

    if g is None:
        return None
    if "x" in g:
        return Point(g["x"], g["y"]) if g["x"] is not None else None
    if "points" in g:
        return MultiPoint([tuple(p[:2]) for p in g["points"]])
    if "paths" in g:
        paths = [LineString([tuple(p[:2]) for p in path]) for path in g["paths"] if len(path) > 1]
        return paths[0] if len(paths) == 1 else MultiLineString(paths)
    if "rings" in g:
        from shapely.geometry import LinearRing

        outers, holes = [], []
        for ring in g["rings"]:
            if len(ring) < 4:
                continue
            lr = LinearRing([tuple(p[:2]) for p in ring])
            (holes if lr.is_ccw else outers).append(Polygon(lr))  # Esri: outer rings clockwise
        if not outers:  # some servers do not follow the convention
            outers, holes = holes, []
        polys = []
        for o in outers:
            inner = [h.exterior for h in holes if o.contains(h.representative_point())]
            polys.append(Polygon(o.exterior, inner).buffer(0))
        return unary_union(polys) if len(polys) > 1 else (polys[0] if polys else None)
    return None


def arcgis_layer_meta(url: str) -> dict:
    return http("GET", url, params={"f": "json"}, timeout=60).json()


def arcgis_resolve(service_or_layer: str, layer_name: str | None = None, layer_id: int | None = None) -> tuple[str, dict, str]:
    """Return (layer_url, layer_metadata, note). Resolves by exact layer name when given (ids can change)."""
    url = service_or_layer.rstrip("/")
    note = ""
    if re.search(r"/(MapServer|FeatureServer)/\d+$", url):
        meta = arcgis_layer_meta(url)
        return url, meta, note
    svc = arcgis_layer_meta(url)
    layers = svc.get("layers", []) + svc.get("tables", [])
    match = None
    if layer_name:
        match = next((l for l in layers if l["name"] == layer_name), None)
        if match is None:
            match = next((l for l in layers if l["name"].lower() == layer_name.lower()), None)
        if match and layer_id is not None and match["id"] != layer_id:
            note = f"layer id changed from {layer_id} to {match['id']} (resolved by name)"
    if match is None and layer_id is not None:
        match = next((l for l in layers if l["id"] == layer_id), None)
        if match and layer_name:
            note = f"layer name {layer_name!r} not found; used id {layer_id} ({match['name']!r})"
    if match is None:
        raise SystemExit(f"Layer {layer_name or layer_id} not in {url}. Layers: {[(l['id'], l['name']) for l in layers]}")
    lurl = f"{url}/{match['id']}"
    return lurl, arcgis_layer_meta(lurl), note


def arcgis_query(run: Run, layer_url: str, bbox=None, where: str = "1=1", out_fields: str = "*"):
    """All features intersecting bbox (run CRS), returned in run CRS (Esri JSON, objectId batching)."""
    import geopandas as gpd

    q = layer_url + "/query"
    base = dict(where=where, f="json", outSR=run.epsg)
    if bbox is not None:
        base.update(geometry=",".join(str(v) for v in bbox), geometryType="esriGeometryEnvelope", inSR=run.epsg,
                    spatialRel="esriSpatialRelIntersects")
    ids = http("POST", q, data={**base, "returnIdsOnly": "true"}).json()
    if "error" in ids:
        raise RuntimeError(f"{layer_url}: {ids['error']}")
    oids = sorted(ids.get("objectIds") or [])
    rows = []
    for i in range(0, len(oids), 500):
        d = http("POST", q, data={**base, "objectIds": ",".join(map(str, oids[i : i + 500])), "outFields": out_fields,
                                  "returnGeometry": "true"}).json()
        if "error" in d:
            raise RuntimeError(f"{layer_url}: {d['error']}")
        for f in d.get("features", []):
            rows.append({**f.get("attributes", {}), "geometry": _esri_geom(f.get("geometry"))})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=run.crs) if rows else gpd.GeoDataFrame({"geometry": []}, crs=run.crs)


def fetch_arcgis(run: Run, name: str, url: str, layer_name: str | None = None, layer_id: int | None = None,
                 bbox=None, where: str = "1=1", provider: str = "", licence: str = "", licence_url: str = "",
                 use: str = "", date_field: str | None = None) -> dict:
    lurl, meta, note = arcgis_resolve(url, layer_name, layer_id)
    gdf = arcgis_query(run, lurl, bbox=bbox, where=where)
    n = run.write_layer(name, gdf) if len(gdf) else 0
    version = ""
    cands = [date_field] + [c for c in gdf.columns if c != "geometry" and re.search(r"stand|update|version|datum|date|jahr|year", c, re.I)]
    parts = []
    for f in cands:
        if f and f in gdf.columns and gdf[f].notna().any():
            vals = gdf[f].dropna()
            from pandas.api.types import is_numeric_dtype

            if is_numeric_dtype(vals) and vals.min() > 1e11:  # Esri dates: epoch milliseconds
                vals = (vals.astype("int64") // 1000).map(lambda s: time.strftime("%Y-%m-%d", time.gmtime(s)))
            lo, hi = vals.astype(str).min(), vals.astype(str).max()
            parts.append(f"{f}: {lo}" + (f" … {hi}" if hi != lo else ""))
        if len(parts) >= 2:
            break
    version = "; ".join(parts)
    rec = dict(name=name, kind="vector", provider=provider, url=lurl, layer=meta.get("name"), count=n,
               geometry_type=meta.get("geometryType"), date_or_version=version or meta.get("description", "")[:120],
               licence=licence or meta.get("copyrightText", ""), licence_url=licence_url, used_for=use,
               request=dict(where=where, bbox=list(bbox) if bbox is not None else None, crs=run.crs), note=note,
               fields=[c for c in gdf.columns if c != "geometry"][:40])
    run.record_source(rec)
    return rec


def _image_tiles(bbox, res, max_px=4000):
    x0, y0, x1, y1 = bbox
    w, h = math.ceil((x1 - x0) / res), math.ceil((y1 - y0) / res)
    nx, ny = math.ceil(w / max_px), math.ceil(h / max_px)
    tw, th = math.ceil(w / nx), math.ceil(h / ny)
    for j in range(ny):
        for i in range(nx):
            tx0 = x0 + i * tw * res
            ty1 = y1 - j * th * res
            cw = min(tw, w - i * tw)
            ch = min(th, h - j * th)
            yield (i * tw, j * th, cw, ch, (tx0, ty1 - ch * res, tx0 + cw * res, ty1))
    return


def _write_rgb(run: Run, rel: str, arr: np.ndarray, bbox, res):
    import rasterio
    from rasterio.transform import from_origin

    target = run.path(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(target, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=3, dtype="uint8",
                       crs=run.crs, transform=from_origin(bbox[0], bbox[3], res, res), compress="deflate", tiled=True) as ds:
        ds.write(arr.transpose(2, 0, 1))
    return target


def fetch_image(run: Run, name: str, url: str, kind: str, layer: str, res: float, bbox, provider="", licence="",
                licence_url="", use="", date_note="", layer_id=None, max_px: int = 4000) -> dict:
    """Georeferenced RGB image from an ArcGIS MapServer export or a WMS GetMap, mosaicked if large."""
    from PIL import Image

    x0, y0, x1, y1 = bbox
    W, H = math.ceil((x1 - x0) / res), math.ceil((y1 - y0) / res)
    mosaic = np.zeros((H, W, 3), dtype=np.uint8)
    layer_label = layer
    if kind == "arcgis_export":
        svc = arcgis_layer_meta(url)
        lid = next((l["id"] for l in svc.get("layers", []) if l["name"] == layer), layer_id)
        if lid is None:
            raise SystemExit(f"Image layer {layer!r} not found in {url}: {[l['name'] for l in svc.get('layers', [])]}")
        layer_label = f"{lid}: {layer}"
    for cx, cy, cw, ch, tb in _image_tiles(bbox, res, max_px):
        if kind == "arcgis_export":
            params = dict(f="image", bbox=",".join(map(str, tb)), bboxSR=run.epsg, imageSR=run.epsg, size=f"{cw},{ch}",
                          format="png", transparent="false", layers=f"show:{lid}")
            r = http("GET", url.rstrip("/") + "/export", params=params, timeout=180)
        else:
            params = dict(service="WMS", request="GetMap", version="1.3.0", layers=layer, styles="", crs=run.crs,
                          bbox=",".join(map(str, tb)), width=cw, height=ch, format="image/png", transparent="false")
            r = http("GET", url, params=params, timeout=180)
        if "image" not in r.headers.get("content-type", ""):
            raise RuntimeError(f"{name}: non-image response {r.text[:300]}")
        im = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB").resize((cw, ch)))
        mosaic[cy : cy + ch, cx : cx + cw] = im
    rel = f"data_raw/{name}.tif"
    _write_rgb(run, rel, mosaic, bbox, res)
    run.register_raster(name, rel)
    rec = dict(name=name, kind="image", provider=provider, url=url, layer=layer_label, resolution_m=res,
               date_or_version=date_note, licence=licence, licence_url=licence_url, used_for=use,
               request=dict(bbox=list(bbox), crs=run.crs, type=kind), file=rel)
    run.record_source(rec)
    return rec


# ============================================================================ OGC


def fetch_wcs(run: Run, name: str, url: str, coverage: str, res: float, bbox, version: str = "1.0.0",
              provider="", licence="", licence_url="", use="", date_note="", max_px: int = 4000) -> dict:
    """DEM (or other single-band coverage) via WCS GetCoverage, tiled and mosaicked to one GeoTIFF."""
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.merge import merge

    x0, y0, x1, y1 = [math.floor(bbox[0] / res) * res, math.floor(bbox[1] / res) * res,
                      math.ceil(bbox[2] / res) * res, math.ceil(bbox[3] / res) * res]
    if version.startswith("1.0"):
        cap = http("GET", url, params=dict(service="WCS", request="GetCapabilities", version="1.0.0"), timeout=60).text
        names = re.findall(r"<(?:\w+:)?name>([^<]+)</(?:\w+:)?name>\s*<(?:\w+:)?label>([^<]+)<", cap)
        if names and coverage not in [n for n, _ in names]:
            hit = next((n for n, lab in names if coverage in lab), None)
            if hit is None:
                raise SystemExit(f"Coverage {coverage!r} not offered. Available: {names}")
            coverage = hit
    datasets = []
    for _, _, cw, ch, tb in _image_tiles((x0, y0, x1, y1), res, max_px):
        if version.startswith("1.0"):
            p = dict(SERVICE="WCS", VERSION="1.0.0", REQUEST="GetCoverage", COVERAGE=coverage, CRS=run.crs, RESPONSE_CRS=run.crs,
                     BBOX=",".join(map(str, tb)), WIDTH=cw, HEIGHT=ch, FORMAT="GeoTIFF")
        else:
            p = [("service", "WCS"), ("version", version), ("request", "GetCoverage"), ("coverageId", coverage),
                 ("subset", f"E({tb[0]},{tb[2]})"), ("subset", f"N({tb[1]},{tb[3]})"),
                 ("subsettingCrs", f"http://www.opengis.net/def/crs/EPSG/0/{run.epsg}"),
                 ("outputCrs", f"http://www.opengis.net/def/crs/EPSG/0/{run.epsg}"), ("format", "image/tiff")]
        r = http("GET", url, params=p, timeout=600)
        if not r.content[:4] in (b"II*\x00", b"MM\x00*"):
            raise RuntimeError(f"{name}: WCS did not return a GeoTIFF: {r.text[:300]}")
        mf = MemoryFile(r.content)
        datasets.append(mf.open())
    arr, transform = merge(datasets)
    nodata = datasets[0].nodata
    for d in datasets:
        d.close()
    band = arr[0].astype("float32")
    if nodata is not None:
        band[band == nodata] = np.nan
    band[(band < -1000) | (band > 9000)] = np.nan
    rel = f"data_raw/{name}.tif"
    with rasterio.open(run.path(rel), "w", driver="GTiff", width=band.shape[1], height=band.shape[0], count=1,
                       dtype="float32", crs=run.crs, transform=transform, nodata=np.nan, compress="deflate", tiled=True) as ds:
        ds.write(band, 1)
    run.register_raster(name, rel)
    if "default_dem" not in run.config and name.startswith("dem"):
        run.config["default_dem"] = name
        run.save_config()
    rec = dict(name=name, kind="raster", provider=provider, url=url, layer=coverage, resolution_m=res,
               date_or_version=date_note, licence=licence, licence_url=licence_url, used_for=use,
               request=dict(bbox=[x0, y0, x1, y1], crs=run.crs, version=version), file=rel,
               stats=dict(min=float(np.nanmin(band)), max=float(np.nanmax(band)), nodata_cells=int(np.isnan(band).sum())))
    run.record_source(rec)
    return rec


def fetch_wfs(run: Run, name: str, url: str, typename: str, bbox, version: str = "2.0.0", provider="", licence="",
              licence_url="", use="", page: int = 5000) -> dict:
    import geopandas as gpd
    import pandas as pd

    frames, start = [], 0
    while True:
        p = dict(service="WFS", request="GetFeature", version=version, srsName=run.crs,
                 bbox=",".join(map(str, bbox)) + f",{run.crs}")
        p["typeNames" if version.startswith("2") else "typeName"] = typename
        if version.startswith("2"):
            p.update(count=page, startIndex=start)
        r = http("GET", url, params=p, timeout=300)
        g = gpd.read_file(io.BytesIO(r.content))
        if g.crs is None:
            g = g.set_crs(run.crs)
        frames.append(g)
        if not version.startswith("2") or len(g) < page:
            break
        start += page
    gdf = pd.concat(frames, ignore_index=True) if frames else gpd.GeoDataFrame({"geometry": []}, crs=run.crs)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs if frames else run.crs)
    n = run.write_layer(name, gdf) if len(gdf) else 0
    rec = dict(name=name, kind="vector", provider=provider, url=url, layer=typename, count=n, licence=licence,
               licence_url=licence_url, used_for=use, request=dict(bbox=list(bbox), crs=run.crs, version=version))
    run.record_source(rec)
    return rec


def fetch_file(run: Run, name: str, url: str, layer: str | None = None, bbox=None, provider="", licence="",
               licence_url="", use="", date_note="", res: float | None = None) -> dict:
    """Vector (gpkg/shp/geojson/zip) or raster (tif) from a URL or a LOCAL path: clip to bbox, reproject, keep one copy.

    Rasters already in the run CRS are clipped without resampling (native values and resolution kept).
    """
    import geopandas as gpd
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    tmpdir = Path(tempfile.mkdtemp(prefix="segmap_"))
    try:
        local = Path(url)
        if local.exists():
            fname = local.resolve()
            url = str(fname)
        else:
            r = http("GET", url, timeout=900, stream=True)
            fname = tmpdir / (url.split("?")[0].rstrip("/").split("/")[-1] or "download")
            with fname.open("wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        path = fname
        if zipfile.is_zipfile(fname):
            with zipfile.ZipFile(fname) as z:
                z.extractall(tmpdir / "x")
            cands = [p for p in (tmpdir / "x").rglob("*") if p.suffix.lower() in (".gpkg", ".shp", ".geojson", ".json", ".tif", ".tiff", ".gml")]
            if not cands:
                raise SystemExit(f"No GIS file inside {url}")
            path = cands[0]
        rec = dict(name=name, provider=provider, url=url, layer=layer or path.name, licence=licence, licence_url=licence_url,
                   used_for=use, date_or_version=date_note)
        if path.suffix.lower() in (".tif", ".tiff", ".asc", ".img", ".vrt") and _same_crs(path, run) and not res:
            with rasterio.open(path) as src:
                from rasterio.windows import from_bounds

                win = from_bounds(*bbox, transform=src.transform).round_offsets().round_lengths() if bbox is not None else None
                if win is not None:
                    win = win.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
                out = src.read(window=win).astype("float32")
                tr = src.window_transform(win) if win is not None else src.transform
                if src.nodata is not None and not np.isnan(src.nodata):
                    out[out == src.nodata] = np.nan
                rel = f"data_raw/{name}.tif"
                with rasterio.open(run.path(rel), "w", driver="GTiff", width=out.shape[2], height=out.shape[1], count=out.shape[0],
                                   dtype="float32", crs=run.crs, transform=tr, nodata=np.nan, compress="deflate", tiled=True) as ds:
                    ds.write(out)
                dst_res = abs(src.res[0])
            run.register_raster(name, rel)
            rec.update(kind="raster", file=rel, resolution_m=dst_res, note="same CRS: clipped, not resampled")
            run.record_source(rec)
            return rec
        if path.suffix.lower() in (".tif", ".tiff", ".asc", ".img", ".vrt"):
            with rasterio.open(path) as src:
                dst_res = res or abs(src.res[0])
                tr, w, h = calculate_default_transform(src.crs, run.crs, src.width, src.height, *src.bounds, resolution=dst_res)
                out = np.full((src.count, h, w), np.nan, dtype="float32")
                for b in range(src.count):
                    reproject(rasterio.band(src, b + 1), out[b], dst_transform=tr, dst_crs=run.crs, resampling=Resampling.bilinear,
                              dst_nodata=np.nan)
                if bbox is not None:
                    from rasterio.windows import from_bounds

                    win = from_bounds(*bbox, transform=tr).round_offsets().round_lengths()
                    r0, c0 = max(0, int(win.row_off)), max(0, int(win.col_off))
                    out = out[:, r0 : r0 + int(win.height), c0 : c0 + int(win.width)]
                    tr = rasterio.windows.transform(rasterio.windows.Window(c0, r0, out.shape[2], out.shape[1]), tr)
                rel = f"data_raw/{name}.tif"
                with rasterio.open(run.path(rel), "w", driver="GTiff", width=out.shape[2], height=out.shape[1], count=out.shape[0],
                                   dtype="float32", crs=run.crs, transform=tr, nodata=np.nan, compress="deflate") as ds:
                    ds.write(out)
            run.register_raster(name, rel)
            rec.update(kind="raster", file=rel, resolution_m=dst_res)
        else:
            g = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
            if path.suffix.lower() in (".json", ".geojson", "") or "query" in url:
                try:
                    head = path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    head = ""
                if '"exceededTransferLimit":true' in head.replace(" ", ""):
                    rec["note"] = ("TRUNCATED: the server returned only its maximum record count. Use `fetch arcgis` "
                                   "(pages through all features) or a narrower where/geometry filter.")
                    print("WARNING:", rec["note"])
            if g.crs is None:
                raise SystemExit(f"{url}: dataset has no CRS")
            g = g.to_crs(run.crs)
            if bbox is not None:
                g = g[g.intersects(_box(bbox))]
            rec.update(kind="vector", count=run.write_layer(name, g))
        run.record_source(rec)
        return rec
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def _box(b):
    from shapely.geometry import box

    return box(*b)


def _same_crs(path, run: Run) -> bool:
    import rasterio

    try:
        with rasterio.open(path) as src:
            return src.crs is not None and src.crs.to_epsg() == run.epsg
    except Exception:
        return False


# ============================================================================ global DEM


def fetch_copernicus_dem(run: Run, bbox, name: str = "dem_glo30", res: float = 30.0) -> dict:
    """Copernicus GLO-30 (global, 1 arc-second DSM) cloud-optimized tiles on AWS Open Data, reprojected to run CRS."""
    import rasterio
    from rasterio.merge import merge
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    lon0, lat0, lon1, lat1 = _bbox_wgs84(run, bbox)
    urls = []
    for lat in range(math.floor(lat0), math.floor(lat1) + 1):
        for lon in range(math.floor(lon0), math.floor(lon1) + 1):
            ns = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00"
            ew = f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00"
            tile = f"Copernicus_DSM_COG_10_{ns}_{ew}_DEM"
            urls.append(f"https://copernicus-dem-30m.s3.amazonaws.com/{tile}/{tile}.tif")
    srcs = []
    for u in urls:
        try:
            srcs.append(rasterio.open(u))
        except Exception as exc:  # ocean tiles do not exist
            print(f"skip {u}: {exc}")
    if not srcs:
        raise SystemExit("No Copernicus DEM tile available for this bbox")
    pad = 0.01
    arr, tr = merge(srcs, bounds=(lon0 - pad, lat0 - pad, lon1 + pad, lat1 + pad))
    src_crs = srcs[0].crs
    for s in srcs:
        s.close()
    h, w = arr.shape[1:]
    left, top = tr.c, tr.f
    right, bottom = left + w * tr.a, top + h * tr.e
    dtr, dw, dh = calculate_default_transform(src_crs, run.crs, w, h, left, bottom, right, top, resolution=res)
    out = np.full((dh, dw), np.nan, dtype="float32")
    reproject(arr[0].astype("float32"), out, src_transform=tr, src_crs=src_crs, dst_transform=dtr, dst_crs=run.crs,
              resampling=Resampling.bilinear, src_nodata=-32767.0, dst_nodata=np.nan)
    rel = f"data_raw/{name}.tif"
    with rasterio.open(run.path(rel), "w", driver="GTiff", width=dw, height=dh, count=1, dtype="float32", crs=run.crs,
                       transform=dtr, nodata=np.nan, compress="deflate", tiled=True) as ds:
        ds.write(out, 1)
    run.register_raster(name, rel)
    if "default_dem" not in run.config:
        run.config["default_dem"] = name
        run.save_config()
    rec = dict(name=name, kind="raster", provider="Copernicus DEM GLO-30 (ESA / Airbus), AWS Open Data", url=urls[0].rsplit("/", 2)[0],
               layer="GLO-30 DSM, 1 arc-second", resolution_m=res, date_or_version="2021 release (data 2011-2015)",
               licence="Copernicus DEM licence (free, attribution: © DLR e.V. 2010-2014 and © Airbus 2014-2018, provided under COPERNICUS by the EU and ESA)",
               licence_url="https://spacedata.copernicus.eu/documents/20123/121286/CSCDA_ESA_Mission-specific+Annex_31_Oct_22.pdf",
               used_for="terrain fallback (surface model: includes buildings/forest)", file=rel, request=dict(tiles=urls, bbox=list(bbox)))
    run.record_source(rec)
    return rec


# ============================================================================ OpenStreetMap

OSM_KEYS = ["building", "highway", "waterway", "railway", "bridge", "landuse", "leisure", "natural", "amenity", "man_made", "place"]


def _overpass_query(b):
    s, w, n, e = b[1], b[0], b[3], b[2]
    bb = f"({s},{w},{n},{e})"
    return f"""[out:json][timeout:180];
(
  way["building"]{bb}; relation["building"]{bb};
  way["highway"]{bb}; way["waterway"]{bb}; way["railway"]{bb};
  way["bridge"]{bb}; way["landuse"]{bb}; way["leisure"]{bb}; way["natural"]{bb}; way["man_made"]{bb};
  relation["type"="multipolygon"]["landuse"]{bb};
  nwr["name"]{bb}; node["place"]{bb};
);
out body geom;"""


def _osm_api_elements(b):
    """Fallback: OSM API 0.6 /map in small tiles (Overpass unavailable)."""
    import xml.etree.ElementTree as ET

    els, seen = [], set()
    step = 0.02
    lon = b[0]
    while lon < b[2]:
        lat = b[1]
        while lat < b[3]:
            q = f"{lon},{lat},{min(lon + step, b[2])},{min(lat + step, b[3])}"
            root = ET.fromstring(http("GET", "https://api.openstreetmap.org/api/0.6/map", params={"bbox": q}, timeout=180).content)
            nodes = {n.attrib["id"]: (float(n.attrib["lon"]), float(n.attrib["lat"])) for n in root.findall("node")}
            for n in root.findall("node"):
                tags = {t.attrib["k"]: t.attrib["v"] for t in n.findall("tag")}
                if tags and ("n", n.attrib["id"]) not in seen:
                    seen.add(("n", n.attrib["id"]))
                    els.append(dict(type="node", id=int(n.attrib["id"]), lon=nodes[n.attrib["id"]][0], lat=nodes[n.attrib["id"]][1], tags=tags))
            for wy in root.findall("way"):
                if ("w", wy.attrib["id"]) in seen:
                    continue
                tags = {t.attrib["k"]: t.attrib["v"] for t in wy.findall("tag")}
                geom = [dict(lon=nodes[nd.attrib["ref"]][0], lat=nodes[nd.attrib["ref"]][1]) for nd in wy.findall("nd") if nd.attrib["ref"] in nodes]
                if tags and len(geom) > 1:
                    seen.add(("w", wy.attrib["id"]))
                    els.append(dict(type="way", id=int(wy.attrib["id"]), tags=tags, geometry=geom))
            lat += step
        lon += step
    return els


def fetch_osm(run: Run, bbox) -> dict:
    import geopandas as gpd
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import polygonize, unary_union

    b = _bbox_wgs84(run, bbox)
    q = _overpass_query(b)
    data, endpoint, failures = None, None, []
    for ep in OVERPASS:
        try:
            r = http("POST", ep, data={"data": q}, timeout=150, retries=1)
            data, endpoint = r.json(), ep
            break
        except Exception as exc:
            failures.append(f"{ep}: {exc}")
    if data is None:
        endpoint = "https://api.openstreetmap.org/api/0.6/map (fallback after Overpass failure)"
        data = {"elements": _osm_api_elements(b), "osm3s": {"timestamp_osm_base": f"live API {today()}"}}
    layers = {k: [] for k in ("osm_buildings", "osm_roads", "osm_waterways", "osm_railways", "osm_areas", "osm_named")}
    for e in data["elements"]:
        t = e.get("tags", {})
        base = dict(osm_id=f"{e['type'][0]}{e['id']}", name=t.get("name"), tags=json.dumps(t, ensure_ascii=False))
        if e["type"] == "node":
            if "lon" in e and (t.get("name") or t.get("place")):
                layers["osm_named"].append({**base, "kind": t.get("place") or next((k for k in OSM_KEYS if k in t), ""), "geometry": Point(e["lon"], e["lat"])})
            continue
        if e["type"] == "way" and e.get("geometry"):
            pts = [(p["lon"], p["lat"]) for p in e["geometry"] if p]
            if len(pts) < 2:
                continue
            closed = len(pts) >= 4 and pts[0] == pts[-1]
            extra = {k: t.get(k) for k in ("highway", "ref", "bridge", "width", "lanes", "layer", "surface", "waterway", "railway", "building", "addr:housenumber")}
            if "building" in t and closed:
                layers["osm_buildings"].append({**base, **extra, "geometry": Polygon(pts)})
            elif "highway" in t:
                layers["osm_roads"].append({**base, **extra, "geometry": LineString(pts)})
            elif "waterway" in t:
                (layers["osm_areas"] if closed and t["waterway"] in ("riverbank", "dock") else layers["osm_waterways"]).append(
                    {**base, **extra, "geometry": Polygon(pts) if closed and t["waterway"] in ("riverbank", "dock") else LineString(pts)})
            elif "railway" in t:
                layers["osm_railways"].append({**base, **extra, "geometry": LineString(pts)})
            elif closed and any(k in t for k in ("landuse", "leisure", "natural", "amenity", "man_made", "bridge")):
                layers["osm_areas"].append({**base, "kind": next(f"{k}={t[k]}" for k in ("landuse", "leisure", "natural", "amenity", "man_made", "bridge") if k in t), "geometry": Polygon(pts)})
            elif t.get("name"):
                layers["osm_named"].append({**base, "kind": "way", "geometry": LineString(pts)})
        elif e["type"] == "relation" and e.get("members"):
            outer, inner = [], []
            for m in e["members"]:
                pts = [(p["lon"], p["lat"]) for p in (m.get("geometry") or []) if p]
                if m.get("type") == "way" and len(pts) > 1:
                    (inner if m.get("role") == "inner" else outer).append(LineString(pts))
            po = unary_union(list(polygonize(unary_union(outer)))) if outer else None
            if po is None or po.is_empty:
                continue
            if inner:
                pi = unary_union(list(polygonize(unary_union(inner))))
                po = po.difference(pi)
            target = "osm_buildings" if "building" in t else "osm_areas"
            layers[target].append({**base, "kind": next((f"{k}={t[k]}" for k in ("building", "landuse", "leisure", "natural") if k in t), "relation"), "geometry": po})
    counts = {}
    for k, rows in layers.items():
        if rows:
            counts[k] = run.write_layer(k, gpd.GeoDataFrame(rows, geometry="geometry", crs=4326))
    rec = dict(name="osm", kind="vector", provider="OpenStreetMap contributors", url=endpoint,
               layer="buildings, highways, waterways, railways, areas, named features", counts=counts,
               date_or_version=f"OSM data timestamp {data.get('osm3s', {}).get('timestamp_osm_base', '')} (current, not historical)",
               licence="ODbL 1.0", licence_url="https://www.openstreetmap.org/copyright",
               used_for="building footprints, roads/bridges, names", request=dict(bbox_wgs84=list(b), query=q if "overpass" in endpoint else "map API tiles"),
               failures=failures)
    run.record_source(rec)
    return rec


# ============================================================================ presets


def fetch_preset(run: Run, preset: str, only: list[str] | None = None, skip: list[str] | None = None) -> list[dict]:
    p = load_preset(preset)
    done = []

    def want(item):
        return (not only or item["name"] in only or item.get("group") in only) and not (skip and (item["name"] in skip or item.get("group") in skip))

    for item in p.get("items", []):
        if not want(item):
            continue
        bbox = bbox_for(run, item.get("bbox", "bbox"))
        common = dict(provider=item.get("provider", p.get("provider", "")), licence=item.get("licence", p.get("licence", "")),
                      licence_url=item.get("licence_url", p.get("licence_url", "")), use=item.get("use", ""))
        try:
            t = item["type"]
            if t == "arcgis":
                rec = fetch_arcgis(run, item["name"], item["service"], item.get("layer_name"), item.get("layer_id"), bbox=bbox,
                                   where=item.get("where", "1=1"), **common)
            elif t == "wcs":
                rec = fetch_wcs(run, item["name"], item["url"], item["coverage"], item["res"], bbox, item.get("version", "1.0.0"),
                                date_note=item.get("date_note", ""), **common)
            elif t in ("arcgis_export", "wms"):
                rec = fetch_image(run, item["name"], item["url"], t, item["layer"], item["res"], bbox, date_note=item.get("date_note", ""), **common)
            elif t == "osm":
                rec = fetch_osm(run, bbox)
            elif t == "copernicus_dem":
                rec = fetch_copernicus_dem(run, bbox, item["name"], item.get("res", 30))
            elif t == "file":
                rec = fetch_file(run, item["name"], item["url"], item.get("layer"), bbox, date_note=item.get("date_note", ""), **common)
            else:
                raise SystemExit(f"Unknown preset item type {t}")
            if item.get("role"):
                rec["role"] = item["role"]
                run.record_source(rec)
            print(f"OK   {item['name']}: {rec.get('count', rec.get('counts', rec.get('file', '')))}", flush=True)
        except Exception as exc:
            rec = dict(name=item["name"], kind="failed", provider=common["provider"], url=item.get("url") or item.get("service"),
                       layer=item.get("layer_name") or item.get("coverage") or item.get("layer"), error=str(exc)[:500],
                       licence=common["licence"], used_for=common["use"])
            run.record_source(rec)
            print(f"FAIL {item['name']}: {exc}", flush=True)
        done.append(rec)
    if p.get("basemaps") and not run.config.get("basemaps"):
        run.config["basemaps"] = p["basemaps"]
        run.save_config()
    return done


# ============================================================================ place lookup


def _variants(term: str) -> list[str]:
    t = term.strip()
    out = {t}
    for a, b in [("ei", "ai"), ("ai", "ei"), ("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("ae", "ä"), ("oe", "ö"), ("ue", "ü")]:
        for v in list(out):
            if a in v.lower():
                out.add(re.sub(a, b, v, flags=re.I))
    if len(t) > 6:
        out.add(t[: max(5, len(t) - 4)])
    return sorted(out)


def locate(run: Run | None, terms: list[str], preset: str | None = None, crs: str | None = None) -> list[dict]:
    """Search official gazetteer layers of a preset (if any) and OSM Nominatim for place/catchment/stream names."""
    from pyproj import Transformer

    target_crs = run.crs if run else (crs or "EPSG:4326")
    results = []
    if preset:
        p = load_preset(preset)
        epsg = int(str(target_crs).split(":")[-1])
        for loc in p.get("locate", []):
            try:
                lurl, _, _ = arcgis_resolve(loc["service"], loc.get("layer_name"), loc.get("layer_id"))
            except SystemExit as exc:
                print(exc)
                continue
            for term in terms:
                likes = " OR ".join(f"UPPER({loc['field']}) LIKE '%{v.upper()}%'" for v in _variants(term))
                d = http("GET", lurl + "/query", params=dict(where=likes, outFields=loc["field"], returnGeometry="true", outSR=epsg,
                                                              f="json", resultRecordCount=50), timeout=60).json()
                for f in d.get("features", [])[:50]:
                    g = _esri_geom(f.get("geometry"))
                    if g is None:
                        continue
                    results.append(dict(source=f"{p['name']}:{loc.get('label', loc.get('layer_name'))}", term=term,
                                        name=f["attributes"].get(loc["field"]), bounds=[round(v, 1) for v in g.bounds], crs=target_crs))
    for term in terms:
        try:
            d = http("GET", "https://nominatim.openstreetmap.org/search", params=dict(q=term, format="json", limit=8), timeout=30).json()
        except Exception as exc:
            print(f"Nominatim failed: {exc}")
            d = []
        t = Transformer.from_crs(4326, target_crs, always_xy=True)
        for h in d:
            s, n, w, e = [float(v) for v in h["boundingbox"]]
            xs, ys = t.transform([w, e], [s, n])
            results.append(dict(source="OSM Nominatim", term=term, name=h.get("display_name"), type=h.get("type"),
                                bounds=[round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)], crs=target_crs))
        time.sleep(1.1)  # Nominatim usage policy
    return results
