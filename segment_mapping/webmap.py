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


def build_map_data(run: Run, popup_html, title: str, context: list[tuple[str, object, str]] | None = None,
                   ticks: list[tuple[str, object]] | None = None) -> dict:
    import geopandas as gpd
    from shapely.geometry import mapping

    from .deliver import _load_evidence

    gdf = gpd.read_file(run.out / "segments.shp", encoding="UTF-8")
    ev = {e["part_id"]: e for e in _load_evidence(run)}
    web = gdf.copy()
    web["geometry"] = web.geometry.simplify(0.3)
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
                          "properties": {"part_id": r.part_id, "hypo": hyp, "big": float(r.area_ha) > 50,
                                         "tooltip": f"{r.part_id} | {r.placement} | {r.confidence}{' | HYPOTHESIS' if hyp else ''} | {r.part_label}",
                                         "popup": popup_html(ev[r.part_id])}})
        segs.append({"name": f"{s['seg_id']}: {s['process'].strip()[:70]}", "color": COLORS[i % len(COLORS)],
                     "features": {"type": "FeatureCollection", "features": feats}})
    ctx = []
    for label, geom, color in context or []:
        gj = gpd.GeoSeries([geom], crs=run.crs).to_crs(4326).iloc[0]
        ctx.append({"label": label, "color": color, "geometry": mapping(gj)})
    tk = []
    if ticks:
        from pyproj import Transformer

        t = Transformer.from_crs(run.crs, 4326, always_xy=True)
        for label, p in ticks:
            lon, lat = t.transform(p.x, p.y)
            tk.append({"label": label, "latlng": [lat, lon]})
    basemaps = run.config.get("basemaps") or [OSM_BASEMAP, ESRI_IMAGERY]
    tiles = []
    for bm in basemaps:
        url = "https://tile.openstreetmap.org/{z}/{x}/{y}.png" if bm["url"] == "OpenStreetMap" else bm["url"]
        tiles.append({"name": bm["name"], "url": url, "attr": bm.get("attr", ""), "wms": bm.get("type") == "wms",
                      "layers": bm.get("layers", ""), "opacity": bm.get("opacity", 1), "max_zoom": bm.get("max_zoom", 19)})
    b = web.total_bounds
    return {"title": title, "segments": segs, "context": ctx, "ticks": tk, "tiles": tiles,
            "offline_default": bool(run.config.get("offline_map", run.config.get("mode") == "text")),
            "hillshade": _hillshade_overlay(run), "bounds": [[b[1], b[0]], [b[3], b[2]]]}


_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__LEAFLET_CSS__</style>
<style>
html,body,#map{height:100%;margin:0}
.pp{font:12px sans-serif;max-height:460px;overflow-y:auto}.pp table.attr th{text-align:left;vertical-align:top;padding-right:6px}.pp p{margin:6px 0}
.legend{background:white;padding:8px 10px;border:1px solid #777;font:12px sans-serif;max-width:340px}
.sw{display:inline-block;width:11px;height:11px;margin-right:4px}
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
    style:function(f){return {color:s.color,weight:2.4,fillColor:s.color,fillOpacity:f.properties.big?0.08:0.28,dashArray:f.properties.hypo?'8 6':null}},
    onEachFeature:function(f,l){l.bindTooltip(f.properties.tooltip);l.bindPopup(f.properties.popup,{maxWidth:620});
      l.on('mouseover',function(){l.setStyle({weight:4})});l.on('mouseout',function(){l.setStyle({weight:2.4})});}
  });
  g.addTo(map);overlays[s.name]=g;
});
L.control.layers(bases,overlays,{collapsed:false}).addTo(map);
L.control.scale().addTo(map);
var lg=L.control({position:'bottomleft'});
lg.onAdd=function(){var d=L.DomUtil.create('div','legend');
  d.innerHTML='<b>'+D.title.replace(/</g,'&lt;')+'</b><br>Solid outline: place identified (is_hypo 0)<br>Dashed outline: hypothesis to check (is_hypo 1)<br>Click a polygon for attributes, evidence, reasoning and uncertainty.<br>'+
  D.segments.map(function(s){return '<span class="sw" style="background:'+s.color+'"></span>'+s.name.split(':')[0]}).join(' ');return d};
lg.addTo(map);
map.fitBounds(D.bounds);
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
                leaflet_inlined="Leaflet 1.9.4" in page_text)
