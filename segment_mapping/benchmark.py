"""Score localization runs against reference runs (frontier-model outputs treated as "mostly true").

python -m segment_mapping.benchmark --reference DIR_OR_FILE [--reference NAME=PATH ...] --run NAME=PATH [...] [--out DIR]

Per segment, every reference model contributes the union of its parts (points/lines buffered by POINT_LINE_BUFFER_M).
- consensus: area covered by >= ceil(n/2) of the n reference models that placed the segment;
- ref_union: union of all reference models;
- ref_sites: connected clusters of ref_union after a SITE_BUFFER_M buffer, kept when >= 2 models support them.
The candidate (union of the run's parts) gets precision against ref_union (+TOLERANCE_M), recall against the consensus
(candidate +TOLERANCE_M), IoU, site recall, area ratio and distances. Segment score = mean(precision, recall, site_recall), scaled down
by the area error outside the scale-mismatch range;
unresolved/missing segments score 0. Each reference is also scored against the others (leave-one-out) as a ceiling.
Parts with geometry_role=search_area say where, not how large: they count for recall and site recall; for precision
they count as the share holding a reference place; they are left out of IoU and the area ratio.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import statistics
from itertools import combinations
from pathlib import Path

import geopandas as gpd
import shapely
from shapely.geometry import Polygon
from shapely.ops import unary_union

POINT_LINE_BUFFER_M = 25.0
TOLERANCE_M = 50.0
SITE_BUFFER_M = 75.0
SCALE_MISMATCH = (0.2, 5.0)
LAYER_NAMES = ("outputs/segments.geojson", "outputs/segments.shp", "segments.geojson", "segments.shp", "outputs/segments.gpkg", "segments.gpkg")
EMPTY = Polygon()


# ---------------------------------------------------------------- geometry helpers
def _flat(g):
    if g is None or g.is_empty:
        return []
    if g.geom_type in ("GeometryCollection", "MultiPolygon", "MultiLineString", "MultiPoint"):
        return [p for sub in shapely.get_parts(g) for p in _flat(sub)]
    return [g]


def _poly(g):
    """Polygonal part of a geometry (drops lines/points produced by overlays)."""
    polys = [p for p in _flat(g) if p.geom_type == "Polygon" and p.area > 0]
    return unary_union(polys) if polys else EMPTY


def footprint(geoms, buffer_m: float = POINT_LINE_BUFFER_M):
    """Union of geometries as an area: polygons kept, points/lines (and degenerate polygons) buffered."""
    out = []
    for g in geoms:
        for p in _flat(shapely.make_valid(g) if g is not None and not g.is_empty else None):
            out.append(p if p.geom_type == "Polygon" else p.buffer(buffer_m))
    return _poly(unary_union(out)) if out else EMPTY


def coverage(geoms, k: int):
    """Area covered by at least k of the geometries."""
    geoms = [g for g in geoms if not g.is_empty]
    if k <= 1:
        return _poly(unary_union(geoms)) if geoms else EMPTY
    if len(geoms) < k:
        return EMPTY
    return _poly(unary_union([shapely.intersection_all(list(c)) for c in combinations(geoms, k)]))


def ref_sites(models: dict, min_support: int = 2):
    """Connected clusters of the reference union (after a SITE_BUFFER_M buffer) supported by >= min_support models."""
    union = unary_union([g for g in models.values() if not g.is_empty])
    if union.is_empty:
        return []
    sites = []
    for comp in _flat(union.buffer(SITE_BUFFER_M)):
        support = sorted(n for n, g in models.items() if g.intersects(comp))
        if len(support) >= min_support:
            sites.append(dict(geometry=_poly(union.intersection(comp)), support=support))
    return sites


# ---------------------------------------------------------------- loading
def norm_id(v) -> str:
    s = str(v).strip()
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else s
    except ValueError:
        return s


def _id_key(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def resolve_layer(path) -> Path:
    p = Path(path)
    if p.is_dir():
        for name in LAYER_NAMES:
            if (p / name).exists():
                return p / name
        raise FileNotFoundError(f"No segments layer in {p} (looked for {', '.join(LAYER_NAMES)})")
    if not p.exists():
        raise FileNotFoundError(p)
    return p


def default_name(path) -> str:
    f = resolve_layer(path)
    d = f.parent.parent if f.parent.name == "outputs" else f.parent
    return d.name


def parse_named(spec: str):
    """NAME=PATH or PATH (name derived from the folder)."""
    if "=" in spec and not Path(spec).exists():
        name, path = spec.split("=", 1)
        return name.strip(), path.strip()
    return default_name(spec), spec


class Layer:
    """Segment parts of one run, grouped per segment, in a metric CRS."""

    def __init__(self, name, path, crs=None):
        self.name, self.file, self.notes = name, resolve_layer(path), []
        gdf = gpd.read_file(self.file)
        if gdf.crs is None:
            self.notes.append(f"{self.file.name}: no CRS; assumed {crs}")
            gdf = gdf.set_crs(crs) if crs else gdf
        if crs is None:
            crs = gdf.crs if gdf.crs and gdf.crs.is_projected else gdf.estimate_utm_crs()
        from .package import _metre_crs
        self.crs = crs = _metre_crs(crs)
        gdf = gdf.to_crs(crs) if gdf.crs is not None and gdf.crs != crs else gdf
        self.statuses = {}
        res = self.file.with_name("segment_results.json")
        if res.exists():
            try:
                self.statuses = {norm_id(r["seg_id"]): r.get("localization_status", "") for r in json.loads(res.read_text(encoding="utf-8-sig"))}
            except (ValueError, KeyError, TypeError) as exc:
                self.notes.append(f"{res.name} unreadable: {exc}")
        if gdf.empty:
            # Empty GeoJSON has no property columns; unresolved IDs come from the ledger.
            self.parts, self.footprints, self.extents, self.search_areas = {}, {}, {}, {}
            return
        if "seg_id" not in gdf:
            raise ValueError(f"{self.file}: no seg_id column")
        gdf["seg_id"] = [norm_id(v) for v in gdf["seg_id"]]
        if "localization_status" in gdf:
            for sid, st in zip(gdf["seg_id"], gdf["localization_status"]):
                if isinstance(st, str) and st:
                    self.statuses.setdefault(sid, st)
            keep = gdf["localization_status"].isna() | (gdf["localization_status"].astype(str).str.lower() == "located")
            gdf = gdf[keep]
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
        self.parts = {sid: list(g.geometry) for sid, g in gdf.groupby("seg_id")}
        self.footprints = {sid: footprint(geoms) for sid, geoms in self.parts.items()}
        # Search areas say where something happened, not its extent: they count for recall and sites, not precision/area.
        search = (gdf["geometry_role"].astype(str).eq("search_area") if "geometry_role" in gdf
                  else gdf["seg_id"].map(lambda _: False)).astype(bool)
        self.extents = {sid: footprint(list(g.geometry)) for sid, g in gdf[~search].groupby("seg_id")}
        self.search_areas = {sid: list(g.geometry) for sid, g in gdf[search].groupby("seg_id")}

    def status(self, sid):
        if sid in self.footprints and not self.footprints[sid].is_empty:
            return "located"
        return "unresolved" if sid in self.statuses else "missing"


# ---------------------------------------------------------------- scoring
def _r(v, nd=3):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), nd)


def build_reference(models: dict, sid: str, exclude=None) -> dict | None:
    """Reference geometries for one segment from {model: Layer}, optionally leaving one model out."""
    fps = {n: L.footprints[sid] for n, L in models.items() if n != exclude and sid in L.footprints and not L.footprints[sid].is_empty}
    if not fps:
        return None
    n = len(fps)
    k = math.ceil(n / 2)
    union = _poly(unary_union(list(fps.values())))
    cons, note = coverage(list(fps.values()), k), ""
    if cons.is_empty:
        cons, note = union, f"no area shared by {k} of {n} models; recall vs reference union"
    return dict(models=sorted(fps), k=k, consensus=cons, union=union, sites=ref_sites(fps, min(2, n)), note=note,
                n_parts=statistics.median(len(models[m].parts[sid]) for m in fps))


def score_segment(cand, ref: dict, status: str, n_parts: int, extent=None, search_areas=()) -> dict:
    """cand: footprint of all parts. extent: footprint without search areas (default cand); search areas only help find
    reference places (recall, sites) and are judged by whether each one contains a reference place."""
    cons, union, sites = ref["consensus"], ref["union"], ref["sites"]
    row = dict(status=status, n_parts=n_parts, ref_n_parts=ref["n_parts"], ref_models=len(ref["models"]), n_sites=len(sites),
               consensus_ha=_r(cons.area / 1e4, 2), cand_ha=None, precision=None, recall=None, iou=None, site_recall=None, sites_hit=None,
               area_ratio=None, nearest_m=None, centroid_m=None, scale_mismatch=False, score=0.0, note=ref["note"], search_areas=len(search_areas))
    if status != "located" or cand.is_empty:
        return row
    extent = cand if extent is None else extent
    cb = cand.buffer(TOLERANCE_M)
    a = extent.area
    row["cand_ha"] = _r(a / 1e4, 2)
    ub = union.buffer(TOLERANCE_M)
    # precision = mean of the extent's area precision and the share of search areas holding a reference place
    checks = [extent.intersection(ub).area / a] if a else []
    if search_areas:
        checks.append(sum(g.intersects(ub) for g in search_areas) / len(search_areas))
    row["precision"] = _r(sum(checks) / len(checks)) if checks else None
    row["recall"] = _r(cb.intersection(cons).area / cons.area) if cons.area else None
    row["iou"] = _r(extent.intersection(cons).area / extent.union(cons).area) if a else None
    if sites:
        hit = sum(1 for s in sites if cb.intersects(s["geometry"]))
        row["sites_hit"], row["site_recall"] = hit, _r(hit / len(sites))
    if cons.area and a:
        ratio = a / cons.area
        row["area_ratio"] = _r(ratio)
        row["scale_mismatch"] = not (SCALE_MISMATCH[0] <= ratio <= SCALE_MISMATCH[1])
    row["nearest_m"] = _r(cand.distance(cons), 1)
    row["centroid_m"] = _r(cand.centroid.distance(cons.centroid), 1)
    vals = [row[k] for k in ("precision", "recall", "site_recall") if row[k] is not None]
    # Covering everything makes recall trivially 1: beyond the mismatch range the score shrinks with the scale error.
    ratio = row["area_ratio"]
    scale = 1.0 if ratio is None or SCALE_MISMATCH[0] <= ratio <= SCALE_MISMATCH[1] else (
        SCALE_MISMATCH[1] / ratio if ratio > SCALE_MISMATCH[1] else ratio / SCALE_MISMATCH[0])
    row["score"] = _r(scale * sum(vals) / len(vals)) if vals else 0.0
    return row


def summarize(rows: dict) -> dict:
    loc = [r for r in rows.values() if r["status"] == "located"]

    def mean(key, rs):
        v = [r[key] for r in rs if r.get(key) is not None]
        return _r(sum(v) / len(v)) if v else None

    return dict(score=mean("score", list(rows.values())), segments=len(rows), located=len(loc),
                unresolved=sum(r["status"] == "unresolved" for r in rows.values()), missing=sum(r["status"] == "missing" for r in rows.values()),
                precision=mean("precision", loc), recall=mean("recall", loc), site_recall=mean("site_recall", loc), iou=mean("iou", loc),
                scale_mismatch=sum(bool(r["scale_mismatch"]) for r in loc))


def score_layer(cand: Layer, refs: dict, segs, exclude=None) -> dict:
    rows = {}
    for sid in segs:
        ref = build_reference(refs, sid, exclude)
        if ref is None:
            continue
        rows[sid] = score_segment(cand.footprints.get(sid, EMPTY), ref, cand.status(sid), len(cand.parts.get(sid, [])),
                                  extent=cand.extents.get(sid, EMPTY), search_areas=cand.search_areas.get(sid, []))
    return dict(summary=summarize(rows), segments=rows)


def benchmark(references, runs, out=None) -> dict:
    """references/runs: lists of (name, path). Returns the result dict; writes benchmark.json/.md when out is given."""
    if not references:
        raise ValueError("at least one reference is needed")
    first = Layer(*references[0])
    refs = {first.name: first}
    for name, path in references[1:]:
        refs[name] = Layer(name, path, first.crs)
    if len(refs) != len(references):
        raise ValueError("reference names must be unique")
    segs = sorted({s for L in refs.values() for s in L.footprints}, key=_id_key)
    notes = [n for L in refs.values() for n in L.notes]
    res = dict(created=dt.datetime.now().isoformat(timespec="seconds"), crs=str(first.crs),
               parameters=dict(point_line_buffer_m=POINT_LINE_BUFFER_M, tolerance_m=TOLERANCE_M, site_buffer_m=SITE_BUFFER_M,
                               scale_mismatch=list(SCALE_MISMATCH)),
               references={n: str(L.file) for n, L in refs.items()}, segments=segs, runs={}, leave_one_out={}, notes=notes)
    for name, path in runs:
        L = Layer(name, path, first.crs)
        r = score_layer(L, refs, segs)
        extra = sorted(set(L.footprints) - set(segs), key=_id_key)
        res["runs"][name] = dict(path=str(L.file), **r, notes=L.notes + ([f"segments not in references (skipped): {', '.join(extra)}"] if extra else []))
    if len(refs) > 1:
        for name, L in refs.items():
            res["leave_one_out"][name] = score_layer(L, refs, segs, exclude=name)
        loo = [v["summary"] for v in res["leave_one_out"].values()]
        res["ceiling"] = {k: _r(statistics.mean(s[k] for s in loo if s[k] is not None))
                          for k in ("score", "precision", "recall", "site_recall", "iou") if any(s[k] is not None for s in loo)}
    if out:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "benchmark.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "benchmark.md").write_text(to_markdown(res), encoding="utf-8")
    return res


# ---------------------------------------------------------------- report
def _f(v, nd=2):
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else ""
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def _summary_row(label, s):
    return (f"| {label} | {_f(s['score'])} | {s['located']}/{s['segments']} | {s['unresolved']} | {s['missing']} | {_f(s['precision'])} | "
            f"{_f(s['recall'])} | {_f(s['site_recall'])} | {_f(s['iou'])} | {s['scale_mismatch']} |")


def to_markdown(res) -> str:
    p = res["parameters"]
    L = ["# Localization benchmark", "",
         f"References ({len(res['references'])}): " + ", ".join(f"`{n}`" for n in res["references"]) + f" · CRS {res['crs']} · {res['created']}",
         f"Points/lines buffered {p['point_line_buffer_m']:g} m; tolerance {p['tolerance_m']:g} m; sites = clusters after {p['site_buffer_m']:g} m "
         f"buffer with >= 2 supporting models; score = mean(precision, recall, site recall) x scale penalty outside the mismatch range, unresolved/missing = 0.", "",
         "## Summary", "", "| run | score | located | unresolved | missing | precision | recall | site recall | IoU | scale mismatch |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    L += [_summary_row(n, r["summary"]) for n, r in res["runs"].items()]
    L += [_summary_row(f"ref {n} (leave-one-out)", r["summary"]) for n, r in res["leave_one_out"].items()]
    if res.get("ceiling"):
        c = res["ceiling"]
        L.append(f"| **inter-reference ceiling (mean LOO)** | {_f(c.get('score'))} | | | | {_f(c.get('precision'))} | {_f(c.get('recall'))} | "
                 f"{_f(c.get('site_recall'))} | {_f(c.get('iou'))} | |")
    for name, r in res["runs"].items():
        L += ["", f"## {name}", "", f"`{r['path']}`", "",
              "| seg | status | parts (run/ref) | precision | recall | sites | IoU | area ratio | cand/cons ha | nearest m | centroid m | score | flags |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for sid, s in r["segments"].items():
            sites = f"{s['sites_hit']}/{s['n_sites']}" if s["sites_hit"] is not None else (f"-/{s['n_sites']}" if s["n_sites"] else "-")
            flags = "; ".join(x for x in ("scale mismatch" if s["scale_mismatch"] else "", s["note"]) if x)
            L.append(f"| {sid} | {s['status']} | {s['n_parts']}/{s['ref_n_parts']:g} | "
                     f"{_f(s['precision'])} | {_f(s['recall'])} | {sites} | {_f(s['iou'])} | {_f(s['area_ratio'])} | "
                     f"{_f(s['cand_ha'], 1)}/{_f(s['consensus_ha'], 1)} | {_f(s['nearest_m'], 0)} | {_f(s['centroid_m'], 0)} | {_f(s['score'])} | {flags} |")
        L += [f"- {n}" for n in r.get("notes", [])]
    if res["leave_one_out"]:
        names = list(res["leave_one_out"])
        L += ["", "## Leave-one-out segment scores (each reference vs the others)", "", "| seg | " + " | ".join(names) + " |",
              "|---" * (len(names) + 1) + "|"]
        for sid in res["segments"]:
            L.append(f"| {sid} | " + " | ".join(_f(res["leave_one_out"][n]["segments"].get(sid, {}).get("score")) for n in names) + " |")
    if res["notes"]:
        L += ["", "Notes:"] + [f"- {n}" for n in res["notes"]]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- command line
def add_arguments(p: argparse.ArgumentParser):
    p.add_argument("--reference", action="append", required=True, metavar="[NAME=]PATH",
                   help="reference run folder or segments file (repeat); the first sets the CRS")
    p.add_argument("--run", dest="runs", action="append", default=[], metavar="NAME=PATH", help="run to score (repeat)")
    p.add_argument("--out", help="output folder (default tmp/benchmark_YYYYMMDD in the project)")


def _shorten(pairs):
    """Drop a common 'Event_' prefix from derived reference names (Seigesbach_Opus -> Opus)."""
    names = [n for n, _ in pairs]
    pre = os.path.commonprefix(names)
    pre = pre[:pre.rfind("_") + 1] if "_" in pre else ""
    return [(n[len(pre):] or n, p) for n, p in pairs] if len(names) > 1 and pre else pairs


def run_cli(a) -> int:
    refs = [parse_named(s) for s in a.reference]
    refs = [x if "=" in s and not Path(s).exists() else y for s, x, y in zip(a.reference, refs, _shorten(refs))]
    runs = [parse_named(s) for s in a.runs]
    out = Path(a.out) if a.out else Path(__file__).resolve().parents[1] / "tmp" / f"benchmark_{dt.date.today():%Y%m%d}"
    benchmark(refs, runs, out)
    print((out / "benchmark.md").read_text(encoding="utf-8"))
    print(f"Wrote {out / 'benchmark.json'} and {out / 'benchmark.md'}")
    return 0


def main(argv=None):
    from .runctx import utf8_stdout

    utf8_stdout()
    p = argparse.ArgumentParser(prog="python -m segment_mapping.benchmark", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p)
    return run_cli(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
