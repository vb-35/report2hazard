"""Command line: python -m segment_mapping <command> RUN_FOLDER [options]. See segment_mapping/README.md."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .runctx import Run, parse_segment_table, today, utf8_stdout, write_json


def _nums(s: str | None):
    if not s:
        return None
    return [float(v) for v in s.replace(";", ",").split(",") if v.strip()]


def _bbox_from_wgs84(crs: str, b):
    from pyproj import Transformer

    t = Transformer.from_crs(4326, crs, always_xy=True)
    xs, ys = t.transform([b[0], b[2], b[0], b[2]], [b[1], b[1], b[3], b[3]])
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _apply_config(run: Run, a) -> None:
    cfg = run.config
    if getattr(a, "preset", None):
        from .sources import load_preset

        p = load_preset(a.preset)
        cfg["preset"] = a.preset
        for k in ("caption_prefixes", "basemaps"):
            if p.get(k):
                cfg[k] = p[k]
        if not getattr(a, "crs", None) and p.get("crs") and not cfg.get("crs_locked"):
            cfg["crs"] = p["crs"]
    if getattr(a, "crs", None):
        if cfg.get("crs_locked") and a.crs != cfg["crs"]:
            raise SystemExit("CRS cannot change after data were downloaded (start a new run folder).")
        cfg["crs"] = a.crs
    if getattr(a, "event", None):
        cfg["event"] = a.event
    for key, arg, argw in (("bbox", "bbox", "bbox_wgs84"), ("detail_bbox", "detail_bbox", "detail_bbox_wgs84")):
        if getattr(a, arg, None):
            cfg[key] = _nums(getattr(a, arg))
        elif getattr(a, argw, None):
            cfg[key] = _bbox_from_wgs84(cfg["crs"], _nums(getattr(a, argw)))
    if getattr(a, "bbox_from", None):
        g = run.layer(a.bbox_from) if a.bbox_from in run.layer_names() else None
        if g is None:
            raise SystemExit(f"--bbox-from: layer {a.bbox_from!r} not found ({run.layer_names()})")
        m = float(a.margin)
        x0, y0, x1, y1 = g.total_bounds
        cfg[a.bbox_target] = [round(x0 - m, 1), round(y0 - m, 1), round(x1 + m, 1), round(y1 + m, 1)]
    if getattr(a, "printed_offset", None) is not None:
        cfg["printed_page_offset"] = int(a.printed_offset)
    if getattr(a, "caption_prefixes", None):
        cfg["caption_prefixes"] = [s.strip() for s in a.caption_prefixes.split(",") if s.strip()]
    if getattr(a, "default_dem", None):
        cfg["default_dem"] = a.default_dem
    run.save_config()


def cmd_init(a):
    run = Run(a.run)
    if (run.dir / "run.json").exists() and not a.force:
        raise SystemExit(f"{run.dir / 'run.json'} exists (use --force to re-initialise the configuration).")
    run.ensure_dirs()
    seg_text = Path(a.segments).read_text(encoding="utf-8-sig")
    segments, header = parse_segment_table(seg_text)
    (run.dir / "scripts" / "segments_input.tsv").write_text(seg_text, encoding="utf-8")
    report = Path(a.report).resolve()
    if not report.exists():
        raise SystemExit(f"Report not found: {report}")
    run.config = dict(event=a.event or segments[0].get("event") or report.stem, report=str(report), crs=a.crs or "EPSG:4326",
                      printed_page_offset=a.printed_offset, segments=segments, segment_header=header, created=today(), rasters={})
    _apply_config(run, a)
    if run.crs == "EPSG:4326":
        print("WARNING: CRS is EPSG:4326. Set the local projected CRS with `config RUN --crs EPSG:xxxx` (or --preset) before fetching data.")
    print(f"Initialised {run.dir}: {len(segments)} segments, CRS {run.crs}, report {report.name} (not copied).")
    for path, name, role in ((a.catchment, "catchment_provided", "catchment"), (a.dem, "dem_provided", "dem")):
        if path:
            ns = argparse.Namespace(run=a.run, path=path, name=name, role=role, layer=None, clip=False, res=None, provider="provided with the task",
                                    licence=None, licence_url=None, use=f"{role} provided with the task", date_note=None, default=True)
            cmd_import(ns)
    run = Run(a.run)  # imports updated run.json
    if a.catchment and not run.config.get("bbox"):
        g = run.layer("catchment_provided")
        x0, y0, x1, y1 = [float(v) for v in g.total_bounds]
        run.config["bbox"] = [round(x0 - 1000, 1), round(y0 - 1000, 1), round(x1 + 1000, 1), round(y1 + 1000, 1)]
        run.save_config()
        print(f"bbox set from the provided catchment + 1000 m: {run.config['bbox']}")


def cmd_import(a):
    """Import a local (provided) vector or raster file into the run: one copy in the run CRS + manifest record."""
    from . import sources as S

    run = Run(a.run)
    run.ensure_dirs()
    if run.crs == "EPSG:4326":
        raise SystemExit("Set a projected CRS first (config RUN --crs EPSG:xxxx).")
    run.config["crs_locked"] = True
    run.save_config()
    bbox = S.bbox_for(run, "bbox") if a.clip else None
    rec = S.fetch_file(run, a.name, a.path, a.layer, bbox, provider=a.provider or "provided with the task", licence=a.licence or "",
                       licence_url=a.licence_url or "", use=a.use or "", date_note=a.date_note or "", res=a.res)
    rec["role"] = a.role or ""
    rec["provided"] = True
    run.record_source(rec)
    if rec.get("kind") == "raster" and (a.role == "dem") and (a.default or not run.config.get("default_dem")):
        run.config["default_dem"] = a.name
        run.save_config()
    _print({x: rec.get(x) for x in ("name", "kind", "count", "file", "resolution_m", "role", "note")})


def cmd_prompt(a):
    """Fill segment_mapping/prompt_template.txt for one mapping task (no run folder is created)."""
    project = Path(__file__).resolve().parents[1]
    template = (Path(__file__).with_name("prompt_template.txt")).read_text(encoding="utf-8")
    report = Path(a.report).resolve()
    if not report.exists():
        raise SystemExit(f"Report not found: {report}")
    seg_text = Path(a.segments).read_text(encoding="utf-8-sig").strip("\r\n")
    parse_segment_table(seg_text)  # fail early on a malformed table
    run_dir = Path(a.run).resolve()
    if (run_dir / "run.json").exists():
        print(f"WARNING: {run_dir} already contains a run; use a new folder per model/run.", file=sys.stderr)

    def describe(path, info, kind):
        if not path:
            return info or "none provided"
        f = Path(path).resolve()
        if not f.exists():
            raise SystemExit(f"{kind} file not found: {f}")
        return f"{f}" + (f" ({info})" if info else "")

    opts = ""
    if a.dem:
        opts += f'--dem "{Path(a.dem).resolve()}" '
    if a.catchment:
        opts += f'--catchment "{Path(a.catchment).resolve()}" '
    out = (template.replace("[segment table]", seg_text)
           .replace("[DEM description]", describe(a.dem, a.dem_info, "DEM"))
           .replace("[catchment description]", describe(a.catchment, a.catchment_info, "Catchment"))
           .replace("[provided data options]", opts)
           .replace("[report PDF path]", str(report))
           .replace("[run folder]", str(run_dir))
           .replace("[project folder]", str(project)))
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
        print(f"Prompt written to {a.out}", file=sys.stderr)
    else:
        print(out)
    print("Reminder: for a blind run, keep worked answers for this report (earlier run folders, answer keys) out of "
          f"{project}.", file=sys.stderr)


def cmd_import_package(a):
    from .package import import_package

    run = Run(a.run)
    run.ensure_dirs()
    _print(import_package(run, a.package))


def cmd_export_package(a):
    from .package import export_package

    print(export_package(Run(a.run), a.out, a.name or ""))


def cmd_import_text(a):
    from .report import import_text, pages_from_pipeline

    run = Run(a.run)
    if a.pipeline_run:
        pages = pages_from_pipeline(a.pipeline_run)
    else:
        pages = json.loads(Path(a.json).read_text(encoding="utf-8"))
    _print(import_text(run, pages))


def cmd_facts(a):
    from .facts import Facts

    run = Run(a.run)
    f = Facts(run, _spec_or_empty(run)).build()
    write_json(run.raw / "facts.json", f)
    for c in f["candidates"]:
        print(f"{c['id']:<4} {c.get('description', '')[:220]}")
    print("Unresolved quoted names:", f["unresolved_quoted_names"])
    print(f"{len(f['numeric_clues'])} numeric clues; full facts in data_raw/facts.json")


def cmd_config(a):
    run = Run(a.run)
    _apply_config(run, a)
    _print({k: v for k, v in run.config.items() if k != "segments"})


def cmd_report(a):
    from .report import extract

    run = Run(a.run)
    r = extract(run, scale=a.scale)
    print(json.dumps(r))
    print(f"Page renders and contact sheets: {run.inspect}. Text: data_raw/report_text.json. Figures: data_raw/report_images.json. "
          f"Captions: data_raw/report_captions.json. Tables: data_raw/report_tables.json.")
    from .runctx import read_json

    for c in read_json(run.raw / "report_captions.json", []):
        print(f"  PDF {c['pdf_page']:>3} / printed {c['printed_page']:>4}: {c['caption'][:150]}")
    for im in read_json(run.raw / "report_images.json", []):
        print(f"  image {im['file']} ({im['width']}x{im['height']})")


def cmd_text(a):
    from .report import search_text

    run = Run(a.run)
    if a.grep:
        for h in search_text(run, a.grep, a.context, a.page):
            print(f"--- PDF {h['pdf_page']} / printed {h['printed_page']} line {h['line']}\n{h['text']}")
    else:
        for p in run.pages():
            if a.page is None or p["pdf_page"] == a.page:
                print(f"=== PDF page {p['pdf_page']} / printed {p['printed_page']}\n{p['text']}")


def cmd_quote(a):
    from .report import resolve_quote

    run = Run(a.run)
    q = {"text": a.text} if a.text else {"start": a.start, "end": a.end}
    if a.page:
        q["pdf_page"] = a.page
    _print(resolve_quote(run, q))


def cmd_render(a):
    from .report import render

    run = Run(a.run)
    print(render(run, a.page, a.image, a.scale, a.crop, a.zoom, a.grid, a.mark, a.polygon, a.out))


def cmd_presets(a):
    from .sources import PRESET_DIR

    for p in sorted(PRESET_DIR.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        print(f"{p.stem}: {d.get('description', '')} (CRS {d.get('crs', 'set per run')})")
        for it in d.get("items", []):
            print(f"    {it['name']:<22} {it['type']:<14} {it.get('role', ''):<16} {it.get('layer_name') or it.get('coverage') or it.get('layer') or ''}")


def cmd_locate(a):
    from .sources import locate

    run = Run(a.run) if a.run else None
    res = locate(run, a.terms, a.preset, a.crs)
    for r in res:
        print(json.dumps(r, ensure_ascii=False))
    if res:
        print("Set the working area with: config RUN --bbox x0,y0,x1,y1 (add a margin) [--detail-bbox ... for OSM/fine DEM/orthophotos]")


def cmd_fetch(a):
    from . import sources as S

    run = Run(a.run)
    run.ensure_dirs()
    if not run.config.get("crs_locked"):
        if run.crs == "EPSG:4326":
            raise SystemExit("Set a projected CRS first (config RUN --crs EPSG:xxxx).")
        run.config["crs_locked"] = True
        run.save_config()
    area = S.bbox_for(run, getattr(a, "area", "bbox") or "bbox")
    common = dict(provider=getattr(a, "provider", "") or "", licence=getattr(a, "licence", "") or "",
                  licence_url=getattr(a, "licence_url", "") or "", use=getattr(a, "use", "") or "")
    k = a.kind
    if k == "preset":
        recs = S.fetch_preset(run, a.preset, a.only.split(",") if a.only else None, a.skip.split(",") if a.skip else None)
        _print([{x: r.get(x) for x in ("name", "kind", "count", "counts", "file", "error", "note")} for r in recs])
        return
    if k == "osm":
        rec = S.fetch_osm(run, area)
    elif k == "dem-global":
        rec = S.fetch_copernicus_dem(run, area, a.name, a.res)
    elif k == "arcgis":
        rec = S.fetch_arcgis(run, a.name, a.url, a.layer_name, a.layer_id, bbox=area, where=a.where, **common)
    elif k == "wfs":
        rec = S.fetch_wfs(run, a.name, a.url, a.typename, area, a.version, **common)
    elif k == "wcs":
        rec = S.fetch_wcs(run, a.name, a.url, a.coverage, a.res, area, a.version, date_note=a.date_note or "", **common)
    elif k == "image":
        rec = S.fetch_image(run, a.name, a.url, a.type, a.layer, a.res, area, date_note=a.date_note or "", **common)
    elif k == "file":
        rec = S.fetch_file(run, a.name, a.url, a.layer, area, date_note=a.date_note or "", res=a.res, **common)
    else:
        raise SystemExit(k)
    if getattr(a, "role", None):
        rec["role"] = a.role
        run.record_source(rec)
    _print({x: rec.get(x) for x in ("name", "kind", "url", "layer", "count", "counts", "file", "date_or_version", "note", "failures")})


def cmd_source(a):
    """Record a source that was consulted without a download (web page, metadata, publication)."""
    run = Run(a.run)
    rec = dict(name=a.name, kind=a.kind, provider=a.provider or "", url=a.url or "", layer=a.layer or "", date_or_version=a.date_note or "",
               licence=a.licence or "", licence_url=a.licence_url or "", used_for=a.use or "", note=a.note or "")
    if a.kind == "failed":
        rec["error"] = a.note or "not accessible"
    run.record_source(rec)
    _print(rec)


def cmd_inventory(a):
    from .inspect_tools import inventory

    print(inventory(Run(a.run)))


def cmd_find(a):
    from .inspect_tools import find

    run = Run(a.run)
    near = tuple(_nums(a.near)) if a.near else None
    for r in find(run, a.pattern, a.layer, near, a.radius, a.limit):
        print(json.dumps(r, ensure_ascii=False, default=str))


def cmd_fields(a):
    run = Run(a.run)
    g = run.layer(a.layer)
    print(f"{a.layer}: {len(g)} features, {g.geom_type.value_counts().to_dict()}")
    for c in g.columns:
        if c == "geometry":
            continue
        vals = g[c].dropna().astype(str).unique()
        print(f"  {c}: {len(vals)} distinct; e.g. {list(vals[:a.n])}")


def cmd_clues(a):
    from .report import clues

    run = Run(a.run)
    p = clues(run, [t.strip() for t in a.terms.split(",")] if a.terms else None)
    print(f"Wrote {p} and data_raw/gazetteer_hits.json")
    from .runctx import read_json

    hits = read_json(run.raw / "gazetteer_hits.json", {})
    print("Gazetteer names found in the report text:")
    for n, src in sorted(hits.items()):
        print(f"  {n}: {', '.join(src)}")


def _spec_or_empty(run):
    from .runctx import SPEC, read_json

    return read_json(run.dir / SPEC, {}) or {}


def cmd_probe(a):
    from .geom import Evaluator
    from .inspect_tools import describe

    run = Run(a.run)
    ev = Evaluator(run, _spec_or_empty(run))
    if a.measure:
        _print(ev.measure(json.loads(a.measure)))
    if a.expr:
        expr = json.loads(a.expr) if a.expr.strip()[:1] in "{[\"" else a.expr
        g = ev.geom(expr, float(a.param) if a.param is not None else None)
        _print(describe(run, g))
        if a.save:
            import geopandas as gpd

            run.plots.mkdir(parents=True, exist_ok=True)
            p = run.plots / f"{a.save}.geojson"
            gpd.GeoDataFrame(geometry=[g], crs=run.crs).to_file(p, driver="GeoJSON")
            print(f"saved {p}")
    if a.chainage:
        _print(ev.chainage(a.chainage).info())


def cmd_plot(a):
    from .inspect_tools import plot

    run = Run(a.run)
    bbox = _nums(a.bbox) if a.bbox else run.bbox("detail_bbox") or run.bbox()
    print(plot(run, bbox, a.layers.split(",") if a.layers else None, a.labels.split(",") if a.labels else None, a.raster,
               a.refs.split("|") if a.refs else None, a.parts, a.out, a.grid, a.title or ""))


def cmd_georef(a):
    from .geom import Evaluator
    from .georef import qa_outputs

    run = Run(a.run)
    spec = run.spec()
    ev = Evaluator(run, spec)
    ids = [a.id] if a.id else list(spec.get("georef", {}))
    out = {}
    for gid in ids:
        gr = ev.georef(gid)
        extra = None
        if a.overlay:
            extra = ev.geom(json.loads(a.overlay) if a.overlay.strip().startswith("{") else a.overlay)
        png = qa_outputs(run, gr, extra=extra)
        info = gr.info()
        out[gid] = dict(method=info["transformation"], gcps=len(info["control_points"]), rms_m=info["rms_m"], max_residual_m=info["max_residual_m"],
                        pixel_m=info["ground_pixel_size_m"], residuals={g.get("label", str(i)): g["residual_m"] for i, g in enumerate(info["control_points"])},
                        checks={c.get("label", str(i)): c["error_m"] for i, c in enumerate(info["independent_checks"])}, qa_png=str(png), note=info["note"])
    _print(out)


def cmd_build(a):
    from .build import build

    run = Run(a.run)
    r = build(run, a.only.split(",") if a.only else None, write=not a.dry)
    for w in r["warnings"]:
        print("warning:", w)
    for e in r["errors"]:
        print("ERROR:", e)
    print(f"{r['parts']} parts built; {len(r['errors'])} errors, {len(r['warnings'])} warnings.")
    if not a.dry and r["parts"]:
        import geopandas as gpd

        g = gpd.read_file(run.out / "segments.shp", encoding="UTF-8")
        print(g.drop(columns="geometry")[["seg_id", "part_id", "placement", "is_hypo", "confidence", "area_ha"]].to_string(index=False))
    return 1 if r["errors"] else 0


def cmd_deliver(a):
    from .deliver import deliver

    run = Run(a.run)
    r = deliver(run, browser_check=not a.no_browser)
    v = r["validation"]
    for w in v["warnings"]:
        print("warning:", w)
    for e in v["errors"]:
        print("ERROR:", e)
    if r["map_check"]:
        print("map check:", json.dumps({k: v2 for k, v2 in r["map_check"].items() if k != "file"}, ensure_ascii=False))
    print(f"Report: {r['report']}\nZIP: {r['zip']}")
    print("Print outputs/data_report.md as the final chat report (it is the same text).")
    return 1 if v["errors"] else 0


def cmd_validate(a):
    from .deliver import validate

    v = validate(Run(a.run))
    _print(v)
    return 1 if v["errors"] else 0


def cmd_serve(a):
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    run = Run(a.run)
    print(f"Serving {run.out} at http://127.0.0.1:{a.port}/segments_map.html (Ctrl+C to stop)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), partial(SimpleHTTPRequestHandler, directory=str(run.out))).serve_forever()


def cmd_benchmark(a):
    from .benchmark import run_cli

    return run_cli(a)


def build_parser():
    p = argparse.ArgumentParser(prog="python -m segment_mapping", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, run=True):
        s = sub.add_parser(name, help=help_)
        if run:
            s.add_argument("run", help="run folder")
        s.set_defaults(fn=fn)
        return s

    def cfg_opts(s):
        s.add_argument("--crs")
        s.add_argument("--preset", help="apply a region preset's CRS, caption words and basemaps")
        s.add_argument("--event")
        s.add_argument("--bbox", help="x0,y0,x1,y1 in run CRS (whole catchment + margin)")
        s.add_argument("--bbox-wgs84", help="lon0,lat0,lon1,lat1")
        s.add_argument("--detail-bbox", help="x0,y0,x1,y1 smaller area for OSM / fine DEM / orthophotos")
        s.add_argument("--detail-bbox-wgs84")
        s.add_argument("--printed-offset", type=int, help="printed page = PDF page + offset")
        s.add_argument("--caption-prefixes", help="comma list, e.g. Abbildung,Tabelle")
        s.add_argument("--default-dem")
        s.add_argument("--bbox-from", help="set a bbox from the extent of a local layer (e.g. catchment_provided)")
        s.add_argument("--bbox-target", choices=["bbox", "detail_bbox"], default="bbox")
        s.add_argument("--margin", type=float, default=1000, help="margin (m) for --bbox-from")

    s = add("init", cmd_init, "create a run folder from a report PDF and a segment table")
    s.add_argument("--report", required=True)
    s.add_argument("--segments", required=True, help="TSV/CSV file with the segment table (header row)")
    s.add_argument("--dem", help="provided DEM file (imported as dem_provided, default DEM)")
    s.add_argument("--catchment", help="provided catchment outline file (imported as catchment_provided; sets bbox)")
    s.add_argument("--force", action="store_true")
    cfg_opts(s)
    s = add("import", cmd_import, "import a provided local file (DEM, catchment, any vector/raster) into the run")
    s.add_argument("path")
    s.add_argument("--name", required=True, help="layer/raster name in the run")
    s.add_argument("--role", help="catchment, streams, roads, bridges, buildings, names, dem, orthophoto, ...")
    s.add_argument("--layer", help="layer inside a multi-layer file")
    s.add_argument("--clip", action="store_true", help="clip to the run bbox")
    s.add_argument("--res", type=float, help="resample rasters to this resolution")
    s.add_argument("--default", action="store_true", help="make this raster the default DEM")
    for o in ("--provider", "--licence", "--licence-url", "--use", "--date-note"):
        s.add_argument(o)
    s = add("prompt", cmd_prompt, "fill the mapping prompt template for a task (prints it, or --out FILE)", run=False)
    s.add_argument("--report", required=True)
    s.add_argument("--run", required=True, help="run folder the model will create (one per model/run)")
    s.add_argument("--segments", required=True, help="TSV/CSV segment table with header row")
    s.add_argument("--dem", help="provided DEM file")
    s.add_argument("--dem-info", help="DEM description (source, resolution, CRS, licence)")
    s.add_argument("--catchment", help="provided catchment outline file")
    s.add_argument("--catchment-info", help="catchment name/code/area and other information")
    s.add_argument("--out", help="write the prompt to this file")
    s = add("import-package", cmd_import_package, "import a spatial package (package.json with layers, rasters, roles, name fields)")
    s.add_argument("package")
    s = add("export-package", cmd_export_package, "write the run's data as a reusable spatial package")
    s.add_argument("out", help="output folder")
    s.add_argument("--name")
    s = add("import-text", cmd_import_text, "use report text from a pipeline run (or a JSON page list) instead of the PDF")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--pipeline-run", help="multi_hazard_pipeline run folder (translated.json)")
    g.add_argument("--json", help="JSON list of pages: {text, translated_text?, file?, page?}")
    add("facts", cmd_facts, "candidate features with ids, name mentions, stream/catchment facts (for constrained localization)")
    s = add("config", cmd_config, "show/change run settings (bbox, CRS, page offset, preset)")
    cfg_opts(s)
    s = add("report", cmd_report, "extract text, tables, captions, figures and page renders from the report")
    s.add_argument("--scale", type=float, default=2.0)
    s = add("text", cmd_text, "print report text (whole, one page, or regex search)")
    s.add_argument("--page", type=int, help="PDF page")
    s.add_argument("--grep")
    s.add_argument("--context", type=int, default=1)
    s = add("quote", cmd_quote, "check that a quotation exists in the report and get its pages")
    s.add_argument("--text")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--page", type=int, help="PDF page hint")
    s = add("render", cmd_render, "render a page/figure crop with pixel grid, markers or a pixel polygon")
    s.add_argument("--page", type=int, help="PDF page to render")
    s.add_argument("--image", help="embedded figure file (see data_raw/report_images.json)")
    s.add_argument("--scale", type=float, default=2.0)
    s.add_argument("--crop", help="x0,y0,x1,y1 in source pixels")
    s.add_argument("--zoom", type=float, default=1.0)
    s.add_argument("--grid", type=float, help="grid spacing in source pixels")
    s.add_argument("--mark", help="x,y;x,y points to mark (source pixels)")
    s.add_argument("--polygon", help="x,y;x,y;... pixel polygon to draw")
    s.add_argument("--out")
    add("presets", cmd_presets, "list region data presets", run=False)
    s = add("locate", cmd_locate, "find catchments/streams/places by name (preset gazetteers + OSM Nominatim)", run=False)
    s.add_argument("terms", nargs="+")
    s.add_argument("--run")
    s.add_argument("--preset")
    s.add_argument("--crs")

    f = add("fetch", lambda a: None, "download data (one copy, run CRS) and record it in the source manifest")
    fs = f.add_subparsers(dest="kind", required=True)

    def fsub(name, help_):
        s = fs.add_parser(name, help=help_)
        s.add_argument("--area", choices=["bbox", "detail"], default="bbox")
        s.add_argument("--provider")
        s.add_argument("--licence")
        s.add_argument("--licence-url")
        s.add_argument("--use", help="what the dataset is used for")
        s.add_argument("--role", help="inventory role: catchment, streams, roads, bridges, buildings, names, dem, orthophoto, ...")
        s.set_defaults(fn=cmd_fetch)
        return s

    s = fsub("preset", "all items of a region preset")
    s.add_argument("preset")
    s.add_argument("--only", help="comma list of item names or groups")
    s.add_argument("--skip")
    fsub("osm", "OpenStreetMap buildings, roads, waterways, areas, names (Overpass; OSM API fallback)")
    s = fsub("dem-global", "Copernicus GLO-30 DEM (global fallback, surface model)")
    s.add_argument("--name", default="dem_glo30")
    s.add_argument("--res", type=float, default=30)
    s = fsub("arcgis", "ArcGIS REST feature layer")
    s.add_argument("--url", required=True, help="MapServer/FeatureServer URL (service or layer)")
    s.add_argument("--layer-name")
    s.add_argument("--layer-id", type=int)
    s.add_argument("--name", required=True)
    s.add_argument("--where", default="1=1")
    s = fsub("wfs", "OGC WFS feature type")
    s.add_argument("--url", required=True)
    s.add_argument("--typename", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--version", default="2.0.0")
    s = fsub("wcs", "OGC WCS coverage (DEM)")
    s.add_argument("--url", required=True)
    s.add_argument("--coverage", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--res", type=float, required=True)
    s.add_argument("--version", default="1.0.0")
    s.add_argument("--date-note")
    s = fsub("image", "orthophoto/map image from ArcGIS MapServer export or WMS")
    s.add_argument("--url", required=True)
    s.add_argument("--type", choices=["arcgis_export", "wms"], required=True)
    s.add_argument("--layer", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--res", type=float, required=True)
    s.add_argument("--date-note")
    s = fsub("file", "direct download of a vector or raster file (zip/gpkg/shp/geojson/tif)")
    s.add_argument("--url", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--layer")
    s.add_argument("--res", type=float)
    s.add_argument("--date-note")

    s = add("source", cmd_source, "record a consulted or unavailable source in the manifest (no download)")
    s.add_argument("--name", required=True)
    s.add_argument("--kind", choices=["reference", "failed"], default="reference")
    for o in ("--provider", "--url", "--layer", "--date-note", "--licence", "--licence-url", "--use", "--note"):
        s.add_argument(o)
    add("inventory", cmd_inventory, "list local spatial data and what is missing (rule 2)")
    s = add("find", cmd_find, "search attribute text in local layers")
    s.add_argument("pattern")
    s.add_argument("--layer")
    s.add_argument("--near", help="x,y")
    s.add_argument("--radius", type=float, default=500)
    s.add_argument("--limit", type=int, default=60)
    s = add("fields", cmd_fields, "list fields and example values of a layer")
    s.add_argument("layer")
    s.add_argument("-n", type=int, default=8)
    s = add("clues", cmd_clues, "geographic clue ledger + gazetteer names found in the report")
    s.add_argument("--terms", help="extra comma-separated terms")
    s = add("probe", cmd_probe, "evaluate a geometry expression / measure / chainage without building")
    s.add_argument("expr", nargs="?", help="ref name or JSON expression")
    s.add_argument("--param", help="value for $param")
    s.add_argument("--measure", help='JSON measure, e.g. {"along_m": {...}}')
    s.add_argument("--chainage", help="print calibration of a chainage")
    s.add_argument("--save", help="save geometry as tmp/plots/NAME.geojson")
    s = add("plot", cmd_plot, "reference PNG with labelled features, refs and built parts")
    s.add_argument("--bbox")
    s.add_argument("--layers", help="comma list of layer names")
    s.add_argument("--labels", help="comma list layer[:field] to label (default field fid_)")
    s.add_argument("--raster", help="raster name (orthophoto) or 'hillshade'")
    s.add_argument("--refs", help="'|'-separated ref names or JSON expressions to overlay")
    s.add_argument("--parts", action="store_true", help="overlay built parts")
    s.add_argument("--grid", type=float, help="coordinate grid spacing (m)")
    s.add_argument("--title")
    s.add_argument("--out", default="plot")
    s = add("georef", cmd_georef, "fit georeferencing from the spec; residuals, checks and QA overlay")
    s.add_argument("--id")
    s.add_argument("--overlay", help="ref/expression to draw on the QA overlay")
    s = add("build", cmd_build, "build shapefile + evidence CSV from scripts/placements.json")
    s.add_argument("--only", help="comma list of part or segment ids (partial build, for testing)")
    s.add_argument("--dry", action="store_true", help="validate without writing")
    s = add("deliver", cmd_deliver, "map, README, data_report.md, reviewer ZIP, validation, browser check")
    s.add_argument("--no-browser", action="store_true")
    add("validate", cmd_validate, "validate outputs")
    s = add("benchmark", cmd_benchmark, "score runs' segment geometries against reference runs (writes benchmark.json/.md)", run=False)
    s.add_argument("--reference", action="append", required=True, metavar="[NAME=]PATH", help="reference run folder or segments file (repeat)")
    s.add_argument("--run", dest="runs", action="append", default=[], metavar="NAME=PATH", help="run to score (repeat)")
    s.add_argument("--out", help="output folder (default tmp/benchmark_YYYYMMDD)")
    s = add("serve", cmd_serve, "serve outputs/ for a manual browser check")
    s.add_argument("--port", type=int, default=8765)
    return p


def main(argv=None):
    utf8_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    a = build_parser().parse_args(argv)
    run_dir = getattr(a, "run", None)
    if run_dir and a.cmd not in ("locate", "prompt") and Path(run_dir, "run.json").exists() or a.cmd == "init":
        try:
            Run(run_dir).log_command(argv)
        except Exception:
            pass
    from .geom import SpecError

    try:
        rc = a.fn(a)
    except SpecError as exc:
        print(f"SPEC ERROR: {exc}", file=sys.stderr)
        return 2
    return rc or 0
