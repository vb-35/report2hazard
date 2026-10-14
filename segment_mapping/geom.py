"""Geometry expression language used by scripts/placements.json (see SPEC.md).

An expression is either a string (name of an entry in spec["refs"]) or a JSON object.
Objects with a "layer" key select features; every other object has exactly one operator key.
The string "$param" anywhere inside an expression is replaced by the part's parameter value.
"""
from __future__ import annotations

import copy
import json
import re

import numpy as np
import shapely
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, box, shape
from shapely.ops import linemerge, substring, unary_union

from .georef import Georef, color_mask_polygon
from .terrain import ChannelHeightModel, FlowModel


class SpecError(ValueError):
    pass


def polygons_only(g):
    """Keep polygonal parts only (drops slivers' lines/points produced by overlays)."""
    if g is None or g.is_empty:
        return Polygon()
    g = shapely.make_valid(g)
    if g.geom_type == "Polygon":
        return g
    if g.geom_type == "MultiPolygon":
        return g
    polys = [p for p in getattr(g, "geoms", []) if p.geom_type in ("Polygon", "MultiPolygon")]
    return unary_union(polys) if polys else Polygon()


def as_line(g):
    if g.geom_type == "LineString":
        return g
    if g.geom_type == "MultiLineString":
        m = linemerge(g)
        if m.geom_type == "LineString":
            return m
        return max(m.geoms, key=lambda p: p.length)
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return as_line(g.boundary)
    raise SpecError(f"Expected a line, got {g.geom_type}")


def as_point(g):
    if g.geom_type == "Point":
        return g
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g.centroid if g.contains(g.centroid) else g.representative_point()
    return g.centroid if g.geom_type != "MultiPoint" else g.centroid


def _substitute(expr, param):
    if param is None:
        return expr
    if isinstance(expr, str):
        if expr == "$param":
            return param
        return expr.replace("$param", repr(param)) if "$param" in expr else expr
    if isinstance(expr, list):
        return [_substitute(v, param) for v in expr]
    if isinstance(expr, dict):
        return {k: _substitute(v, param) for k, v in expr.items()}
    return expr


class Chainage:
    """Report stationing calibrated to a GIS line (line starts at the stationing origin).

    Between anchors: piecewise-linear scaling; beyond the outer anchors: constant translation.
    """

    def __init__(self, name, line: LineString, unit_m: float, anchors: list[dict]):
        self.name, self.line, self.unit = name, line, float(unit_m)
        self.anchors = sorted(anchors, key=lambda a: a["value"])
        for a in self.anchors:
            a["offset_m"] = round(a["raw_m"] - a["value"] * self.unit, 3)

    def raw(self, v: float) -> float:
        A = self.anchors
        if len(A) == 1 or v <= A[0]["value"]:
            a = A[0]
            return a["raw_m"] + (v - a["value"]) * self.unit
        if v >= A[-1]["value"]:
            a = A[-1]
            return a["raw_m"] + (v - a["value"]) * self.unit
        for a, b in zip(A, A[1:]):
            if a["value"] <= v <= b["value"]:
                t = (v - a["value"]) / (b["value"] - a["value"])
                return a["raw_m"] + t * (b["raw_m"] - a["raw_m"])
        raise AssertionError

    def value(self, raw: float) -> float:
        A = self.anchors
        if len(A) == 1 or raw <= A[0]["raw_m"]:
            a = A[0]
            return a["value"] + (raw - a["raw_m"]) / self.unit
        if raw >= A[-1]["raw_m"]:
            a = A[-1]
            return a["value"] + (raw - a["raw_m"]) / self.unit
        for a, b in zip(A, A[1:]):
            if a["raw_m"] <= raw <= b["raw_m"]:
                t = (raw - a["raw_m"]) / (b["raw_m"] - a["raw_m"])
                return a["value"] + t * (b["value"] - a["value"])
        raise AssertionError

    def info(self) -> dict:
        segs = []
        for a, b in zip(self.anchors, self.anchors[1:]):
            segs.append(dict(between=[a["value"], b["value"]], scale=round((b["raw_m"] - a["raw_m"]) / ((b["value"] - a["value"]) * self.unit), 5)))
        return dict(name=self.name, unit_m=self.unit, line_length_m=round(self.line.length, 3),
                    line_length_units=round(self.value(self.line.length), 3), anchors=self.anchors, scales=segs,
                    method="piecewise-linear between anchors, constant translation beyond the outer anchors; anchors are calibration, not validation")


class Evaluator:
    def __init__(self, run, spec: dict):
        self.run = run
        self.spec = spec
        self.refs = spec.get("refs", {})
        self._cache: dict = {}
        self._stack: list = []
        self.ref_fits: dict = {}
        self._models: dict = {}
        self._flow: dict = {}
        self._georefs: dict = {}
        self._chainages: dict = {}

    # ================================================================== entry points
    def geom(self, expr, param=None):
        expr = _substitute(expr, param)
        if isinstance(expr, str):
            return self.ref(expr)
        if isinstance(expr, list):
            raise SpecError(f"Expected a geometry expression, got a list: {expr!r:.120}")
        if not isinstance(expr, dict):
            raise SpecError(f"Expected a geometry expression, got {expr!r:.120}")
        if "layer" in expr:
            return self._layer(expr)
        if len(expr) != 1:
            raise SpecError(f"Expression must have exactly one operator key (or 'layer'): {list(expr)}")
        op, arg = next(iter(expr.items()))
        fn = getattr(self, f"op_{op}", None)
        if fn is None:
            raise SpecError(f"Unknown operator {op!r}. Known: {sorted(k[3:] for k in dir(self) if k.startswith('op_'))}")
        try:
            return fn(arg)
        except SpecError:
            raise
        except Exception as exc:
            raise SpecError(f"{op}: {exc}") from exc

    def ref(self, name: str):
        if name in self._cache:
            return self._cache[name]
        if name not in self.refs:
            raise SpecError(f"Unknown ref {name!r}. Defined refs: {sorted(self.refs)}")
        if name in self._stack:
            raise SpecError(f"Circular ref: {' -> '.join(self._stack + [name])}")
        self._stack.append(name)
        try:
            entry = self.refs[name]
            if isinstance(entry, dict) and "expr" in entry:
                from .build import evaluate_parameter

                g, fit = evaluate_parameter(self, entry["expr"], entry.get("parameter"), label=f"ref {name}")
                if fit:
                    self.ref_fits[name] = fit
            else:
                g = self.geom(entry)
        finally:
            self._stack.pop()
        self._cache[name] = g
        return g

    def number(self, v):
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict) and "measure" in v:
            return float(self.measure(v["measure"])["value"])
        raise SpecError(f"Expected a number, got {v!r}")

    # ================================================================== feature selection
    def _layer(self, e: dict):
        gdf = self.run.layer(e["layer"])
        sel = gdf
        if "ids" in e:
            field = e.get("id_field", "fid_")
            want = [str(v) for v in e["ids"]]
            sel = sel[sel[field].astype(str).isin(want)]
            missing = set(want) - set(sel[field].astype(str))
            if missing:
                raise SpecError(f"{e['layer']}: {field} not found: {sorted(missing)}")
        for field, cond in (e.get("where") or {}).items():
            if field not in sel.columns:
                raise SpecError(f"{e['layer']}: no field {field!r}. Fields: {[c for c in gdf.columns if c != 'geometry']}")
            sel = sel[_where(sel[field], cond)]
        if "intersects" in e:
            g = self.geom(e["intersects"])
            sel = sel[sel.intersects(g)]
        if "within_distance" in e:
            g = self.geom(e["within_distance"][0])
            sel = sel[sel.distance(g) <= float(e["within_distance"][1])]
        if "within" in e:
            g = self.geom(e["within"])
            sel = sel[sel.representative_point().within(g)]
        if sel.empty:
            hints = {f: sorted(gdf[f].dropna().astype(str).unique().tolist())[:40] for f in (e.get("where") or {}) if f in gdf.columns}
            raise SpecError(f"No feature in {e['layer']} matches {json.dumps({k: v for k, v in e.items() if k != 'layer'}, ensure_ascii=False)[:300]}. Values: {hints}")
        merge = e.get("merge", "union")
        geoms = list(sel.geometry)
        if e.get("explode"):
            geoms = [p for g in geoms for p in getattr(g, "geoms", [g])]
        if merge == "nearest":
            ref = self.geom(e["nearest_to"])
            return min(geoms, key=lambda g: g.distance(ref))
        if merge == "largest":
            return max(geoms, key=lambda g: g.area or g.length)
        if merge == "first":
            if len(geoms) > 1:
                raise SpecError(f"{e['layer']}: merge 'first' but {len(geoms)} features match; refine 'where' or use 'ids'")
            return geoms[0]
        u = unary_union(geoms)
        if merge == "lines":
            return as_line(u)
        return u

    def op_stream(self, a: dict):
        """Merged line oriented by DEM so that it starts at its LOW end (mouth) unless orient='down'."""
        g = self._layer({k: v for k, v in a.items() if k not in ("orient", "clip_to", "dem")} | {"merge": "union"})
        dem = self.run.dem(a.get("dem"))
        b = dem.bounds
        k = 2 * dem.res
        dem_box = box(b.left + k, b.bottom + k, b.right - k, b.top - k)

        def merge(geom):
            m = linemerge(geom) if geom.geom_type == "MultiLineString" else geom
            return max(m.geoms, key=lambda p: p.length) if m.geom_type == "MultiLineString" else m

        if "clip_to" in a:
            g = g.intersection(dem_box if a["clip_to"] == "dem" else self.geom(a["clip_to"]))
        merged = merge(g)
        z0, z1 = dem.sample(*merged.coords[0]), dem.sample(*merged.coords[-1])
        if np.isnan(z0) or np.isnan(z1):  # line leaves the DEM: orient on the part inside it
            merged = merge(g.intersection(dem_box))
            if merged.is_empty:
                raise SpecError(f"stream {a.get('where')} lies outside the DEM")
            z0, z1 = dem.sample(*merged.coords[0]), dem.sample(*merged.coords[-1])
            if np.isnan(z0) or np.isnan(z1):
                raise SpecError(f"stream {a.get('where')}: DEM has no data at the line ends")
        low_first = z0 <= z1
        if (a.get("orient", "up") == "up") != low_first:
            merged = LineString(list(merged.coords)[::-1])
        return merged

    # ================================================================== literals
    def op_point(self, a):
        return Point(*[float(v) for v in a[:2]])

    def op_point_wgs84(self, a):
        from pyproj import Transformer

        x, y = Transformer.from_crs(4326, self.run.crs, always_xy=True).transform(float(a[0]), float(a[1]))
        return Point(x, y)

    def op_polygon(self, a):
        return Polygon([tuple(map(float, p)) for p in a]).buffer(0)

    def op_line(self, a):
        return LineString([tuple(map(float, p)) for p in a])

    def op_wkt(self, a):
        return shapely.from_wkt(a)

    def op_bbox(self, a):
        return box(*[float(v) for v in a])

    def op_file(self, a):
        import geopandas as gpd

        if isinstance(a, str):
            a = {"path": a}
        g = gpd.read_file(self.run.path(a["path"]), layer=a.get("layer")).to_crs(self.run.crs)
        return unary_union(list(g.geometry))

    def op_raster_pixel(self, a):
        """Point at a pixel position (col,row; continuous) of a registered raster, e.g. an orthophoto."""
        import rasterio

        with rasterio.open(self.run.raster_path(a["raster"])) as ds:
            x, y = ds.transform @ (float(a["pixel"][0]), float(a["pixel"][1]))
        return Point(x, y)

    # ================================================================== point/line helpers
    def op_start(self, a):
        return Point(as_line(self.geom(a)).coords[0])

    def op_end(self, a):
        return Point(as_line(self.geom(a)).coords[-1])

    def op_endpoint_near(self, a):
        ln = as_line(self.geom(a["line"]))
        to = self.geom(a["to"])
        return min((Point(ln.coords[0]), Point(ln.coords[-1])), key=lambda p: p.distance(to))

    def op_centroid(self, a):
        return self.geom(a).centroid

    def op_representative_point(self, a):
        return self.geom(a).representative_point()

    def op_nearest_point(self, a):
        """Point on geometry `on` nearest to `to`."""
        return shapely.ops.nearest_points(self.geom(a["on"]), self.geom(a["to"]))[0]

    def _origin(self, ln, a):
        return ln.project(as_point(self.geom(a["origin"]))) if a.get("origin") is not None else 0.0

    def op_interpolate(self, a):
        ln = as_line(self.geom(a["line"]))
        return ln.interpolate(self._origin(ln, a) + self.number(a["at_m"]))

    def op_substring(self, a):
        ln = as_line(self.geom(a["line"]))
        o = self._origin(ln, a)
        d0, d1 = o + self.number(a["from_m"]), o + self.number(a["to_m"])
        return substring(ln, max(0.0, d0), min(ln.length, d1))

    def op_between(self, a):
        ln = as_line(self.geom(a["line"]))
        p0 = ln.project(as_point(self.geom(a["from"])))
        p1 = ln.project(as_point(self.geom(a["to"])))
        lo, hi = sorted((p0, p1))
        ext = a.get("extend_m", [0, 0])
        return substring(ln, max(0.0, lo - float(ext[0])), min(ln.length, hi + float(ext[1])))

    def op_window(self, a):
        ln = as_line(self.geom(a["line"]))
        c = ln.project(as_point(self.geom(a["center"]))) + float(a.get("offset_m", 0))
        half = self.number(a["length_m"]) / 2
        return substring(ln, max(0.0, c - half), min(ln.length, c + half))

    # ================================================================== chainage
    def chainage(self, name: str) -> Chainage:
        if name in self._chainages:
            return self._chainages[name]
        cfg = self.spec.get("chainages", {}).get(name)
        if cfg is None:
            raise SpecError(f"Unknown chainage {name!r}. Defined: {sorted(self.spec.get('chainages', {}))}")
        ln = as_line(self.geom(cfg["line"]))
        anchors = []
        for an in cfg.get("anchors", [{"value": 0, "at": "start"}]):
            if an.get("at") == "start":
                raw = 0.0
            else:
                raw = ln.project(as_point(self.geom(an["point"])))
            anchors.append(dict(value=float(an["value"]), raw_m=round(raw, 3), label=an.get("label", "")))
        ch = Chainage(name, ln, cfg.get("unit_m", 100), anchors)
        self._chainages[name] = ch
        return ch

    def op_chainage_point(self, a):
        ch = self.chainage(a["chainage"])
        return ch.line.interpolate(ch.raw(self.number(a["at"])))

    def op_reach(self, a):
        ch = self.chainage(a["chainage"])
        r0, r1 = sorted((ch.raw(self.number(a["from"])), ch.raw(self.number(a["to"]))))
        if r1 > ch.line.length + 1e-6 and not a.get("allow_clip", True):
            raise SpecError(f"reach end beyond line ({r1:.1f} > {ch.line.length:.1f} m)")
        return substring(ch.line, max(0.0, r0), min(ch.line.length, r1))

    # ================================================================== constructive
    def op_buffer(self, a):
        g = self.geom(a["geom"])
        d = self.number(a["width"]) / 2 if "width" in a else self.number(a["distance"])
        return g.buffer(d, cap_style=a.get("cap", "round"), join_style=a.get("join", "round"),
                        single_sided=bool(a.get("single_sided", False)))

    def op_union(self, a):
        return unary_union([self.geom(x) for x in a])

    def op_intersection(self, a):
        gs = [self.geom(x) for x in a]
        out = gs[0]
        for g in gs[1:]:
            out = out.intersection(g)
        return out

    def op_difference(self, a):
        gs = [self.geom(x) for x in a]
        out = gs[0]
        for g in gs[1:]:
            out = out.difference(g)
        return out

    def op_largest(self, a):
        g = polygons_only(self.geom(a))
        return max(getattr(g, "geoms", [g]), key=lambda p: p.area)

    def op_polygons_only(self, a):
        return polygons_only(self.geom(a))

    def op_simplify(self, a):
        return self.geom(a["geom"]).simplify(float(a["tolerance"]))

    def op_convex_hull(self, a):
        return self.geom(a).convex_hull

    def op_fill_holes(self, a):
        g = polygons_only(self.geom(a))
        return unary_union([Polygon(p.exterior) for p in getattr(g, "geoms", [g])])

    def op_half_plane(self, a):
        """Polygon on one side of a line: side = north|south|east|west (geographic) or left|right (of line direction)."""
        ln = as_line(self.geom(a["line"]))
        ext = float(a.get("extent_m", 5000))
        ee = float(a.get("end_extension_m", ext))  # prolong the line ends sideways (0 = close straight from the ends)
        side = a["side"]
        if side in ("left", "right"):
            return ln.buffer(ext if side == "left" else -ext, single_sided=True, cap_style="flat")
        c = list(ln.coords)
        if side in ("north", "south"):
            if c[0][0] > c[-1][0]:
                c = c[::-1]
            s = ext if side == "north" else -ext
            ring = [(c[0][0] - ee, c[0][1])] + c + [(c[-1][0] + ee, c[-1][1]), (c[-1][0] + ee, c[-1][1] + s), (c[0][0] - ee, c[0][1] + s)]
        elif side in ("east", "west"):
            if c[0][1] > c[-1][1]:
                c = c[::-1]
            s = ext if side == "east" else -ext
            ring = [(c[0][0], c[0][1] - ee)] + c + [(c[-1][0], c[-1][1] + ee), (c[-1][0] + s, c[-1][1] + ee), (c[0][0] + s, c[0][1] - ee)]
        else:
            raise SpecError("half_plane side must be north/south/east/west/left/right")
        return Polygon(ring).buffer(0)

    def op_nearest_part(self, a):
        """Portion of `geom` whose raster cells (cell_m) are nearer to lines[index] than to the other lines.

        Parts for different indices partition the geometry exactly (they never overlap and their union is `geom`).
        """
        from rasterio.features import shapes
        from rasterio.transform import from_origin

        g = self.geom(a["geom"])
        lines = [self.geom(x) for x in a["lines"]]
        k = int(a["index"])
        cell = float(a.get("cell_m", 5))
        x0, y0, x1, y1 = g.bounds
        x0, y0, x1, y1 = x0 - cell, y0 - cell, x1 + cell, y1 + cell
        w, h = int(np.ceil((x1 - x0) / cell)), int(np.ceil((y1 - y0) / cell))
        cc, rr = np.meshgrid(np.arange(w), np.arange(h))
        pts = shapely.points(x0 + (cc.ravel() + 0.5) * cell, y1 - (rr.ravel() + 0.5) * cell)
        d = np.vstack([shapely.distance(pts, ln) for ln in lines])
        m = (np.argmin(d, axis=0) == k).reshape(h, w)
        tr = from_origin(x0, y1, cell, cell)
        cells = unary_union([shape(s) for s, v in shapes(m.astype("uint8"), mask=m, transform=tr) if v == 1])
        return g.intersection(cells)

    # ================================================================== terrain
    def op_dem_mask(self, a):
        dem = self.run.dem(a.get("dem"))
        m = dem.valid.copy()
        if "within" in a:
            within = self.geom(a["within"])
            m &= dem.mask(within)
        if a.get("min") is not None:
            m &= dem.z >= self.number(a["min"])
        if a.get("max") is not None:
            m &= dem.z <= self.number(a["max"])
        g = dem.polygonize(m)
        return g.intersection(within) if "within" in a else g

    def _model(self, a):
        key = json.dumps({k: v for k, v in a.items() if k != "height_m"}, sort_keys=True, ensure_ascii=False)
        if key not in self._models:
            lines = [self.geom(x) for x in a["channels"]]
            within = self.geom(a["within"]) if a.get("within") is not None else None
            self._models[key] = ChannelHeightModel(self.run.dem(a.get("dem")), lines, float(a["max_dist_m"]), within=within,
                                                   bed_window_m=float(a.get("bed_window_m", 8)), step_m=a.get("step_m"),
                                                   monotone=bool(a.get("monotone", False)), connected=bool(a.get("connected", True)))
        return self._models[key]

    def op_terrain(self, a):
        return self._model(a).geom(self.number(a["height_m"]))

    def op_watershed(self, a):
        dem = self.run.dem(a.get("dem"))
        if dem.name not in self._flow:
            self._flow[dem.name] = FlowModel(dem)
        fm = self._flow[dem.name]
        p = as_point(self.geom(a["pour_point"]))
        r, c = fm.snap(p.x, p.y, float(a.get("snap_m", 2 * dem.res)))
        return dem.polygonize(fm.watershed(r, c))

    # ================================================================== georeferenced report figures
    def georef(self, gid: str) -> Georef:
        if gid in self._georefs:
            return self._georefs[gid]
        cfg = copy.deepcopy(self.spec.get("georef", {}).get(gid))
        if cfg is None:
            raise SpecError(f"Unknown georef {gid!r}. Defined: {sorted(self.spec.get('georef', {}))}")
        cfg.setdefault("target_crs", self.run.crs)

        def resolve(items):
            out = []
            for g in items:
                g = dict(g)
                t = g["target"]
                g["target_spec"] = t
                if isinstance(t, list) and len(t) == 2 and all(isinstance(v, (int, float)) for v in t):
                    tgt = [float(t[0]), float(t[1])]
                elif isinstance(t, dict) and "source" in t:
                    from pyproj import Transformer

                    x, y = Transformer.from_crs(t["crs"], self.run.crs, always_xy=True).transform(*t["source"])
                    tgt = [x, y]
                else:
                    p = as_point(self.geom(t))
                    tgt = [p.x, p.y]
                g["target"] = [round(tgt[0], 3), round(tgt[1], 3)]
                out.append(g)
            return out

        gr = Georef(gid, cfg, resolve(cfg.get("gcps", [])), resolve(cfg.get("checks", [])))
        self._georefs[gid] = gr
        return gr

    def op_georef_pixels(self, a):
        gr = self.georef(a["georef"])
        pts = [tuple(map(float, p)) for p in a["pixels"]]
        kind = a.get("type", "polygon")
        g = Polygon(pts) if kind == "polygon" else LineString(pts) if kind == "line" else Point(pts[0])
        out = gr.transform_geom(g)
        return out.buffer(0) if kind == "polygon" else out

    def op_georef_color(self, a):
        gr = self.georef(a["georef"])
        image = self.run.path(a.get("image") or gr.cfg["image"])
        pix, _ = color_mask_polygon(image, a["rule"], a.get("exclude_px"), a.get("include_px"), int(a.get("min_pixels", 4)),
                                    int(a.get("close_px", 0)), int(a.get("open_px", 0)))
        return gr.transform_geom(pix).buffer(0)

    # ================================================================== measurements
    def measure(self, m: dict) -> dict:
        """{"area_ha": expr} | {"area_km2"} | {"length_m"} | {"distance_m": [a, b]} | {"along_m": {"line","from","to"}}
        | {"chainage": {"chainage": name, "geom": expr}} | {"chainage_range": {...}} | {"elevation_m": expr}
        | {"dem_stats": expr} | {"count": expr} | {"bounds": expr} | {"width_m": {"geom": polygon, "length_m": L}}
        | {"gradient_pct": {"line", "from", "to", "bed_window_m"}}"""
        if len(m) != 1:
            raise SpecError(f"measure needs exactly one key, got {list(m)}")
        kind, arg = next(iter(m.items()))
        if kind == "area_ha":
            return dict(value=self.geom(arg).area / 1e4, unit="ha")
        if kind == "area_km2":
            return dict(value=self.geom(arg).area / 1e6, unit="km²")
        if kind == "area_m2":
            return dict(value=self.geom(arg).area, unit="m²")
        if kind == "length_m":
            return dict(value=self.geom(arg).length, unit="m")
        if kind == "distance_m":
            return dict(value=self.geom(arg[0]).distance(self.geom(arg[1])), unit="m")
        if kind == "along_m":
            ln = as_line(self.geom(arg["line"]))
            return dict(value=abs(ln.project(as_point(self.geom(arg["to"]))) - ln.project(as_point(self.geom(arg["from"])))), unit="m")
        if kind in ("chainage", "chainage_range"):
            ch = self.chainage(arg["chainage"])
            g = self.geom(arg["geom"])
            if kind == "chainage":
                p = as_point(g)
                return dict(value=ch.value(ch.line.project(p)), unit=f"{ch.name} units ({ch.unit:g} m)", lateral_m=p.distance(ch.line))
            vals = [ch.value(ch.line.project(Point(c))) for c in shapely.get_coordinates(g)]
            return dict(value=[min(vals), max(vals)], unit=f"{ch.name} units")
        if kind == "elevation_m":
            p = as_point(self.geom(arg if not isinstance(arg, dict) or "geom" not in arg else arg["geom"]))
            dem = self.run.dem(arg.get("dem") if isinstance(arg, dict) and "geom" in arg else None)
            return dict(value=dem.sample(p.x, p.y), unit="m a.s.l.", dem=dem.name)
        if kind == "dem_stats":
            g = self.geom(arg if not (isinstance(arg, dict) and "geom" in arg) else arg["geom"])
            dem = self.run.dem(arg.get("dem") if isinstance(arg, dict) and "geom" in arg else None)
            s = dem.stats(g)
            return dict(value=[s.get("min"), s.get("max")], unit="m a.s.l. (min, max)", **s)
        if kind == "gradient_pct":
            ln = as_line(self.geom(arg["line"]))
            dem = self.run.dem(arg.get("dem"))
            pa, pb = as_point(self.geom(arg["from"])), as_point(self.geom(arg["to"]))
            w = float(arg.get("bed_window_m", 2 * dem.res))
            za, zb = dem.window_min(pa.x, pa.y, w), dem.window_min(pb.x, pb.y, w)
            run_m = abs(ln.project(pb) - ln.project(pa))
            return dict(value=100 * abs(zb - za) / run_m if run_m else float("nan"), unit="% (mean along line)",
                        z_from=round(za, 2), z_to=round(zb, 2), along_m=round(run_m, 2))
        if kind == "count":
            g = self.geom(arg)
            return dict(value=len(getattr(g, "geoms", [g])), unit="parts")
        if kind == "bounds":
            return dict(value=[round(v, 2) for v in self.geom(arg).bounds], unit=self.run.crs)
        if kind == "width_m":
            return dict(value=self.geom(arg["geom"]).area / self.number(arg["length_m"]), unit="m (area / length)")
        raise SpecError(f"Unknown measure {kind!r}")


def _where(series, cond):
    s = series
    if not isinstance(cond, dict):
        return s.astype(str) == str(cond)
    (op, v), = cond.items()
    st = s.astype(str)
    if op == "eq":
        return st == str(v)
    if op == "ne":
        return st != str(v)
    if op == "in":
        return st.isin([str(x) for x in v])
    if op == "not_in":
        return ~st.isin([str(x) for x in v])
    if op == "contains":
        return st.str.contains(str(v), case=True, regex=False, na=False)
    if op == "icontains":
        return st.str.contains(str(v), case=False, regex=False, na=False)
    if op == "startswith":
        return st.str.startswith(str(v), na=False)
    if op == "regex":
        return st.str.contains(v, flags=re.I, regex=True, na=False)
    if op == "isnull":
        return s.isna() if v else s.notna()
    num = s.astype(float)
    return {"lt": num < v, "le": num <= v, "gt": num > v, "ge": num >= v}[op]
