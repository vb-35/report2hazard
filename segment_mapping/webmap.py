"""Self-contained interactive map (Leaflet inlined, data embedded): works offline; web base maps are optional."""
from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path

import numpy as np

from .runctx import ESRI_IMAGERY, OSM_BASEMAP, Run

VENDOR = Path(__file__).with_name("vendor") / "leaflet"
COLORS = ["#1b9e77", "#7570b3", "#e7298a", "#d95f02", "#1f78b4", "#b2182b", "#17a2b8", "#6a3d9a", "#b8860b", "#2ca02c",
          "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#ff7f0e"]


def _hillshade_overlay(run: Run, max_px: int = 1600) -> dict | None:
    """DEM hillshade warped to Web Mercator as a PNG data URI with its lat/lon bounds (offline background)."""
    try:
        dem = run.dem()
    except Exception:
        return None
    import rasterio
    from PIL import Image
    from pyproj import Transformer
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    z = dem.z.copy()
    step = max(1, int(np.ceil(max(z.shape) / max_px)))
    z = z[::step, ::step]
    res = dem.res * step
    filled = np.where(np.isfinite(z), z, np.nanmin(z) if np.isfinite(z).any() else 0)
    gy, gx = np.gradient(filled, res)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    alt, az = np.radians(45), np.radians(315)
    hs = np.clip(np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect), 0, 1)
    hs = (hs * 255).astype("uint8")
    alpha = np.where(np.isfinite(z), 255, 0).astype("uint8")
    src_tr = dem.transform * dem.transform.scale(step, step)
    h, w = hs.shape
    left, top = src_tr.c, src_tr.f
    right, bottom = left + w * src_tr.a, top + h * src_tr.e
    dtr, dw, dh = calculate_default_transform(dem.crs, "EPSG:3857", w, h, left, bottom, right, top)
    out = np.zeros((2, dh, dw), "uint8")
    for i, band in enumerate((hs, alpha)):
        reproject(band, out[i], src_transform=src_tr, src_crs=dem.crs, dst_transform=dtr, dst_crs="EPSG:3857",
                  resampling=Resampling.bilinear)
    img = Image.fromarray(np.dstack([out[0], out[0], out[0], out[1]]), "RGBA")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    t = Transformer.from_crs(3857, 4326, always_xy=True)
    x0, y1 = dtr.c, dtr.f
    x1, y0 = x0 + dw * dtr.a, y1 + dh * dtr.e
    lon0, lat0 = t.transform(x0, y0)
    lon1, lat1 = t.transform(x1, y1)
    return {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), "bounds": [[lat0, lon0], [lat1, lon1]]}


_FAMILY = {"Polygon": "polygon", "MultiPolygon": "polygon", "LineString": "line", "MultiLineString": "line",
           "LinearRing": "line", "Point": "point", "MultiPoint": "point"}


def _flat_parts(geom) -> list:
    import shapely

    out = []
    for p in shapely.get_parts(geom):
        out.extend(_flat_parts(p) if p.geom_type in ("GeometryCollection", "MultiPolygon", "MultiLineString", "MultiPoint") else [p])
    return out


def clean_geometry(geom, family: str | None = None):
    """Valid geometry of one dimension (polygon, line or point) for display.

    Reprojection to longitude/latitude and simplification can make a valid outline invalid (a near-zero-width hole
    or sliver collapses to a line, rings self-touch). Repair with make_valid keeping the input structure, drop collapsed
    remnants and keep only parts of the original dimension. Returns an empty geometry when nothing usable remains."""
    import shapely
    from shapely.geometry import GeometryCollection, MultiLineString, MultiPoint, MultiPolygon
    from shapely.ops import unary_union

    if geom is None or geom.is_empty:
        return GeometryCollection()
    family = family or _FAMILY.get(geom.geom_type)
    g = geom
    if not g.is_valid:
        g = shapely.make_valid(g, method="structure", keep_collapsed=False) if family == "polygon" else shapely.make_valid(g)
    parts = [q for q in _flat_parts(g) if _FAMILY.get(q.geom_type) == family and not q.is_empty]
    if family == "polygon":
        parts = [q for q in parts if q.area > 0]
    elif family == "line":
        parts = [q for q in parts if q.length > 0]
    if not parts:
        return GeometryCollection()
    if len(parts) == 1:
        out = parts[0]
    else:
        out = {"polygon": MultiPolygon, "line": MultiLineString, "point": MultiPoint}[family](parts)
    if not out.is_valid and family == "polygon":
        out = unary_union(parts)
    return out if out.is_valid and not out.is_empty and _FAMILY.get(out.geom_type) == family else GeometryCollection()


def web_geometries(geoms, crs, tolerance: float = 0.3, fallback=None) -> list:
    """Simplify geometries (given in the projected run CRS) and project them to EPSG:4326, repairing after each step.

    ``fallback`` holds EPSG:4326 geometries used when the simplified result cannot be repaired (e.g. the validated
    canonical GeoJSON geometry). Items that cannot be made valid come back as None."""
    import geopandas as gpd

    geoms = list(geoms)
    fams = [_FAMILY.get(g.geom_type) if g is not None and not g.is_empty else None for g in geoms]
    proj = gpd.GeoSeries([clean_geometry(g, f) for g, f in zip(geoms, fams)], crs=crs)
    if tolerance:
        proj = gpd.GeoSeries([clean_geometry(g, f) for g, f in zip(proj.simplify(tolerance), fams)], crs=crs)
    web = proj.to_crs(4326)
    out = []
    for i, (g, f) in enumerate(zip(web, fams)):
        g = clean_geometry(g, f) if f else None
        if (g is None or g.is_empty) and fallback is not None and f:
            g = clean_geometry(fallback[i], f)
        out.append(g if g is not None and not g.is_empty and g.is_valid else None)
    return out


def invalid_embedded(data: dict) -> list[str]:
    """Labels of embedded segment features and context overlays whose geometry is empty or invalid."""
    from shapely.geometry import shape

    bad = []
    items = [(f["properties"].get("part_id"), f["geometry"]) for s in data.get("segments", []) for f in s["features"]["features"]]
    items += [(f"context {c.get('label')}", c["geometry"]) for c in data.get("context", [])]
    for label, gj in items:
        try:
            g = shape(gj)
            ok = g.is_valid and not g.is_empty
        except Exception:
            ok = False
        if not ok:
            bad.append(str(label))
    return bad


def build_map_data(run: Run, popup_html, title: str, context: list[tuple[str, object, str]] | None = None,
                   ticks: list[tuple[str, object]] | None = None) -> dict:
    import geopandas as gpd
    from shapely.geometry import mapping

    from .deliver import _load_evidence, load_geometries

    gdf = load_geometries(run)
    ev = {e["part_id"]: e for e in _load_evidence(run)}
    web = gdf.copy()
    if len(web):
        # Same validated canonical geometry as segments.geojson, simplified for display in the run CRS and repaired
        # after simplification and reprojection (falls back to the unsimplified canonical geometry).
        web = web.to_crs(4326)
        fixed = web_geometries(web.to_crs(run.crs).geometry, run.crs, 0.3, fallback=list(web.geometry))
        bad = [pid for pid, g in zip(web.part_id, fixed) if g is None]
        if bad:
            raise ValueError(f"map: no valid web geometry for parts {bad}")
        web = web.set_geometry(gpd.GeoSeries(fixed, index=web.index, crs=4326))
    web = web.to_crs(4326)
    segs = []
    for i, s in enumerate(run.segments):
        sub = web[web.seg_id.astype(str) == s["seg_id"]].copy()
        if sub.empty:
            continue
        sub = sub.sort_values("area_ha", ascending=False)
        feats = []
        for _, r in sub.iterrows():
            hyp = int(r.is_hypo) == 1
            feats.append({"type": "Feature", "geometry": mapping(r.geometry),
                          "properties": {"part_id": r.part_id, "hypo": hyp, "big": bool(r.area_ha is not None and float(r.area_ha) > 50),
                                         "role": ev[r.part_id].get("geometry_role", "event_extent"),
                                         "boundary_confidence": ev[r.part_id].get("boundary_confidence", "unknown"),
                                         "tooltip": html.escape(f"{r.part_id} | {ev[r.part_id].get('geometry_role', 'event_extent')} | boundary {ev[r.part_id].get('boundary_confidence', 'unknown')} | {r.placement} | {r.confidence}{' | HYPOTHESIS' if hyp else ''} | {r.part_label}"),
                                         "popup": popup_html(ev[r.part_id])}})
        segs.append({"name": html.escape(f"{s['seg_id']}: {s['process'].strip()[:70]}"), "color": COLORS[i % len(COLORS)],
                     "features": {"type": "FeatureCollection", "features": feats}})
    ctx = []
    context = list(context or [])
    for (label, _, color), gj in zip(context, web_geometries([g for _, g, _ in context], run.crs, 0.3)):
        if gj is None:
            print(f"map context {label}: no valid geometry after reprojection; omitted")
            continue
        ctx.append({"label": html.escape(str(label)), "color": color, "geometry": mapping(gj)})
    tk = []
    if ticks:
        from pyproj import Transformer

        t = Transformer.from_crs(run.crs, 4326, always_xy=True)
        for label, p in ticks:
            lon, lat = t.transform(p.x, p.y)
            tk.append({"label": html.escape(str(label)), "latlng": [lat, lon]})
    basemaps = run.config.get("basemaps") or [OSM_BASEMAP, ESRI_IMAGERY]
    tiles = []
    for bm in basemaps:
        url = "https://tile.openstreetmap.org/{z}/{x}/{y}.png" if bm["url"] == "OpenStreetMap" else bm["url"]
        tiles.append({"name": html.escape(str(bm["name"])), "url": url, "attr": bm.get("attr", ""), "wms": bm.get("type") == "wms",
                      "layers": bm.get("layers", ""), "opacity": bm.get("opacity", 1), "max_zoom": bm.get("max_zoom", 19)})
    from .runctx import read_json
    outcomes = read_json(run.out / "segment_results.json", [])
    if len(web):
        b = web.total_bounds
        bounds = [[float(b[1]), float(b[0])], [float(b[3]), float(b[2])]]
    elif ctx:
        from shapely.geometry import shape
        b = gpd.GeoSeries([shape(c["geometry"]) for c in ctx], crs=4326).total_bounds
        bounds = [[float(b[1]), float(b[0])], [float(b[3]), float(b[2])]]
    else:
        bounds = [[-60, -180], [75, 180]]
    data = {"title": title, "segments": segs, "context": ctx, "ticks": tk, "tiles": tiles,
            "offline_default": bool(run.config.get("offline_map", run.config.get("mode") == "text")),
            "hillshade": _hillshade_overlay(run), "bounds": bounds, "segment_results": outcomes}
    bad = invalid_embedded(data)
    if bad:
        raise ValueError(f"map: invalid embedded geometries {bad}")
    return data


_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__LEAFLET_CSS__</style>
<style>
html,body,#map{height:100%;margin:0}
.pp{font:12px sans-serif;max-height:460px;overflow-y:auto}.pp table.attr th{text-align:left;vertical-align:top;padding-right:6px}.pp p{margin:6px 0}
.legend{background:white;padding:6px 9px;border:1px solid #777;font:12px/1.35 sans-serif;box-sizing:border-box;
  width:max-content;max-width:min(340px,calc(100vw - 70px));max-height:calc(100vh - 140px);overflow:auto}
.legend summary{cursor:pointer}.legend details>summary{list-style-position:inside}
.legend .lg-sec{margin-top:5px}.legend ul{margin:3px 0 0;padding-left:16px}.legend li{margin-bottom:3px}
.legend .ln{display:inline-block;width:22px;height:0;border-top:2px solid #333;vertical-align:middle;margin-right:5px}
.legend .ln.dash{border-top-style:dashed}.legend .ln.dot{border-top-style:dotted;border-top-color:#888}
.legend .chips{display:flex;flex-wrap:wrap;gap:2px 8px}
.leaflet-control-layers{max-width:min(380px,calc(100vw - 70px))}
.sw{display:inline-block;width:11px;height:11px;margin-right:4px;vertical-align:middle}
</style></head>
<body><div id="map"></div>
<script id="segmap-data" type="application/json">__DATA__</script>
<script>__LEAFLET_JS__</script>
<script>
(function(){
var D=JSON.parse(document.getElementById('segmap-data').textContent);
var map=L.map('map',{preferCanvas:false});
var bases={},overlays={};
var blank=L.layerGroup();bases['No base map (offline)']=blank;
D.tiles.forEach(function(t){
  var lyr=t.wms?L.tileLayer.wms(t.url,{layers:t.layers,format:'image/png',transparent:true,attribution:t.attr,opacity:t.opacity})
              :L.tileLayer(t.url,{attribution:t.attr,maxZoom:t.max_zoom});
  if(t.wms){overlays[t.name]=lyr}else{bases[t.name]=lyr}
});
var firstTile=Object.keys(bases).filter(function(k){return k!=='No base map (offline)'})[0];
(D.offline_default||!firstTile?blank:bases[firstTile]).addTo(map);
if(D.hillshade){var hs=L.imageOverlay(D.hillshade.url,D.hillshade.bounds,{opacity:0.55});overlays['Hillshade from run DEM (offline)']=hs;
  if(D.offline_default||!firstTile){hs.addTo(map)}}
if(D.context.length||D.ticks.length){var cg=L.layerGroup();
  D.context.forEach(function(c){L.geoJSON(c.geometry,{style:{color:c.color,weight:1.5,fillOpacity:0}}).bindTooltip(c.label).addTo(cg)});
  D.ticks.forEach(function(t){L.circleMarker(t.latlng,{radius:3,color:'black',fillOpacity:1}).bindTooltip(t.label).addTo(cg)});
  cg.addTo(map);overlays['Context (reference features, not event outlines)']=cg;}
D.segments.forEach(function(s){
  var g=L.geoJSON(s.features,{
    pointToLayer:function(f,latlng){return L.circleMarker(latlng,{radius:6,color:s.color,fillOpacity:0.8})},
    style:function(f){var role=f.properties.role;return {color:s.color,weight:2.4,fillColor:s.color,
      fillOpacity:role==='context'||role==='search_area'?0.03:(f.properties.big?0.08:0.28),
      dashArray:f.properties.hypo?'8 6':(role==='context'||role==='search_area'?'3 5':null)}},
    onEachFeature:function(f,l){l.bindTooltip(f.properties.tooltip);l.bindPopup(f.properties.popup,{maxWidth:620});
      l.on('mouseover',function(){l.setStyle({weight:4})});l.on('mouseout',function(){l.setStyle({weight:2.4})});}
  });
  g.addTo(map);overlays[s.name]=g;
});
L.control.layers(bases,overlays,{collapsed:false}).addTo(map);
L.control.scale().addTo(map);
var lg=L.control({position:'bottomleft'});
function esc(x){return String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
lg.onAdd=function(){var d=L.DomUtil.create('div','legend');
  L.DomEvent.disableClickPropagation(d);L.DomEvent.disableScrollPropagation(d);
  var unresolved=(D.segment_results||[]).filter(function(r){return r.localization_status==='unresolved'});
  d.innerHTML='<details class="lg-main"'+(window.innerHeight>=420?' open':'')+'><summary><b>'+esc(D.title)+'</b> (legend)</summary>'+
  '<div class="lg-sec"><span class="ln"></span>place identified (is_hypo 0)<br><span class="ln dash"></span>hypothesis to check (is_hypo 1)<br>'+
  '<span class="ln dot"></span>context or search area<br>Points and lines: location without inferred area.<br>'+
  'Click a feature for attributes, evidence and boundary confidence.</div>'+
  '<div class="lg-sec"><b>Segments</b><div class="chips">'+(D.segments.length?D.segments.map(function(s){
    return '<span title="'+s.name+'"><span class="sw" style="background:'+s.color+'"></span>'+s.name.split(':')[0]+'</span>'}).join(''):'none located')+'</div></div>'+
  '<div class="lg-sec">'+(unresolved.length?'<details class="lg-unres"><summary>Unresolved segments ('+unresolved.length+')</summary><ul>'+
    unresolved.map(function(r){return '<li><b>'+esc(r.seg_id)+'</b>: '+esc(r.reason)+'</li>'}).join('')+'</ul></details>':'Unresolved segments: none')+'</div>'+
  '</details>';return d};
lg.addTo(map);
map.fitBounds(D.bounds,{maxZoom:16});
})();
</script></body></html>
"""


def write_map(run: Run, data: dict) -> Path:
    js = (VENDOR / "leaflet.js").read_text(encoding="utf-8")
    css = (VENDOR / "leaflet.css").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = (_TEMPLATE.replace("__LEAFLET_CSS__", css).replace("__TITLE__", html.escape(data["title"]))
            .replace("__DATA__", payload).replace("__LEAFLET_JS__", js))
    target = run.out / "segments_map.html"
    target.write_text(page, encoding="utf-8")
    return target


def embedded_counts(page_text: str) -> dict:
    import re

    m = re.search(r'<script id="segmap-data" type="application/json">(.*?)</script>', page_text, re.S)
    if not m:
        return {}
    d = json.loads(m.group(1).replace("<\\/", "</"))
    feats = [f for s in d["segments"] for f in s["features"]["features"]]
    return dict(segment_layers=len(d["segments"]), parts=len(feats), hypotheses=sum(f["properties"]["hypo"] for f in feats),
                popups=sum(bool(f["properties"]["popup"]) for f in feats), hillshade=bool(d.get("hillshade")),
                context_layers=len(d.get("context", [])), invalid_geometries=invalid_embedded(d),
                leaflet_inlined="Leaflet 1.9.4" in page_text)
