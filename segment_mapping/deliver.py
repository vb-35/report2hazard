"""Deliverables: interactive map, README, data_report.md, reviewer ZIP, final validation, browser check."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np

from . import __version__
from .build import SHP_FIELDS, _shp_text
from .geom import Evaluator
from .runctx import Run, read_json, today, write_json

ZIP_FILES = ["segments.shp", "segments.shx", "segments.dbf", "segments.prj", "segments.cpg", "segments_evidence.csv",
             "segments_map.html", "README.md"]
FIELD_HELP = {
    "seg_id": "Segment ID exactly as in the input table",
    "part_id": "Unique part key (joins segments_evidence.csv)",
    "part_label": "Short description of what the part represents",
    "process": "Process (from the segment table)",
    "category": "Generalized category (from the segment table)",
    "interact": "Interaction type (from the segment table)",
    "phase": "Sediment transport phase (from the segment table)",
    "placement": "How the geometry was built: documented | approximate | modelled | hypothesis",
    "is_hypo": "1 = reviewer should check which place/feature is meant or the extent has no basis in the report; 0 = identified",
    "confidence": "Confidence in the LOCATION of the part: high | medium | low",
    "geom_basis": "Method used to build the polygon (full text in the CSV)",
    "data_src": "GIS layers and report figures used",
    "area_ha": "Polygon area in hectares (planar, in the shapefile CRS)",
    "rep_value": "Value given in the report for comparison (area, length, ...)",
    "rep_page": "Report page(s) (printed page numbers unless stated otherwise)",
}


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_") or "event"


def _load_evidence(run: Run) -> list[dict]:
    csv.field_size_limit(50_000_000)
    with (run.out / "segments_evidence.csv").open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _j(s, default):
    try:
        return json.loads(s) if s else default
    except json.JSONDecodeError:
        return default


def _md(v) -> str:
    return str(v if v is not None else "").replace("|", "/").replace("\n", " ").strip()


def _blank(v) -> str:
    return "" if v is None else str(v)


def table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    out += ["| " + " | ".join(_md(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


# ============================================================================ map


def _popup_html(e: dict) -> str:
    esc = html.escape
    rows = "".join(f"<tr><th>{esc(k)}</th><td>{esc(str(e.get(k, '')))}</td></tr>" for k in SHP_FIELDS)
    parts = [f"<h4>{esc(e['part_id'])} — {esc(e['part_label'])}{' <span style=\"color:#b00\">(HYPOTHESIS — check)</span>' if e['is_hypo'] == '1' else ''}</h4>",
             f"<table class='attr'>{rows}</table>"]

    def sec(title, body):
        if body and str(body).strip():
            parts.append(f"<p><b>{esc(title)}</b><br>{body}</p>")

    sec("Report quotations (written evidence)", esc(e.get("quotations", "")).replace("\n\n", "<br><br>"))
    vis = _j(e.get("visual_observations_json"), [])
    sec("Visual evidence (analyst observations, not quotations)", "<br>".join(
        esc(f"Fig. {v.get('figure', '')} (PDF p. {v.get('pdf_page', '')}, printed p. {v.get('printed_page', '')}, {v.get('evidence_type', '')}): "
            f"caption „{v.get('caption_quotation', '')}“ — {v.get('observation', '')} [date/relevance: {v.get('date_relevance', '')}]")
        if isinstance(v, dict) else esc(str(v)) for v in vis))
    tabs = _j(e.get("table_observations_json"), [])
    sec("Table evidence", "<br>".join(esc(f"Table {t.get('table')} (printed p. {t.get('printed_page', '')}): {t.get('observation', '')}") for t in tabs))
    sec("Image date / event relevance", esc(e.get("image_date_relevance", "")))
    sec("Visual identification method", esc(e.get("visual_identification_method", "")))
    geo = _j(e.get("georeferencing_json"), {})
    sec("Georeferencing", "<br>".join(esc(
        f"{k}: {g.get('transformation')} {g.get('source_crs', '')} → {g.get('target_crs', '')}, {len(g.get('control_points', []))} GCPs, "
        f"RMS {g.get('rms_m')} m, max {g.get('max_residual_m')} m; independent checks: "
        + (", ".join(f"{c.get('label', '')} {c.get('error_m')} m" for c in g.get("independent_checks", [])) or "none"))
        for k, g in geo.items()) + ("<br>" + esc(e.get("georeferencing_limitations", "")) if e.get("georeferencing_limitations") else ""))
    sec("Reasoning", esc(e.get("reasoning", "")))
    sec("Uncertainty", esc(e.get("uncertainty", "")))
    sec("Alternative readings", esc(e.get("alternative_hypotheses", "")))
    sec("Classification", esc(f"placement: {e.get('reason_placement', '')} | is_hypo: {e.get('reason_is_hypo', '')} | confidence: {e.get('reason_confidence', '')}"))
    fits = _j(e.get("fit_json"), {})
    sec("Area fit (not an independent check)", "<br>".join(esc(
        f"{k}: target {f.get('target_ha')} ha → {f.get('fitted_ha')} ha ({f.get('error_pct')}%) at {f.get('parameter_name', 'parameter')} = {f.get('parameter_value')} {f.get('unit', '')}")
        for k, f in fits.items()))
    sens = _j(e.get("sensitivity_json"), [])
    par = _j(e.get("parameter_json"), {})
    sec(f"Sensitivity{' (rule 5d)' if e.get('rule_5d') == '1' else ''}: {par.get('name', '')} {par.get('unit', '')}",
        esc(", ".join(f"{s['value']} → {s['area_ha']} ha" for s in sens)))
    chk = _j(e.get("checks_json"), [])
    sec("Checks", "<br>".join(esc(f"{c.get('label')}: GIS {c.get('value')} {c.get('unit', '')} vs report {c.get('report_value', '')} {c.get('report_text', '')}") for c in chk))
    return "<div class='pp'>" + "".join(parts) + "</div>"


def make_map(run: Run) -> Path:
    from .webmap import build_map_data, write_map

    spec = run.spec()
    context, ticks = [], []
    ctx_items = (spec.get("map") or {}).get("context", [])
    tick_cfg = (spec.get("map") or {}).get("chainage_ticks")
    if ctx_items or tick_cfg:
        evl = Evaluator(run, spec)
        for c in ctx_items:
            try:
                context.append((c.get("label", ""), evl.geom(c["geom"]), c.get("color", "#444")))
            except Exception as exc:
                print(f"map context {c.get('label')}: {exc}")
        if tick_cfg:
            ch = evl.chainage(tick_cfg["chainage"])
            vals = tick_cfg.get("values") or list(np.arange(0, ch.value(ch.line.length) + 1e-9, tick_cfg.get("every", 1)))
            for v in vals:
                ticks.append((f"{tick_cfg['chainage']} {float(v):g}", ch.line.interpolate(min(ch.raw(float(v)), ch.line.length))))
    data = build_map_data(run, _popup_html, spec.get("event") or run.config.get("event", ""), context, ticks)
    return write_map(run, data)


def _browser() -> str | None:
    cands = [os.environ.get("SEGMAP_BROWSER", ""),
             r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    for c in cands:
        if c and Path(c).exists():
            return c
    for name in ("chromium", "chromium-browser", "google-chrome", "msedge", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def check_map(run: Run, wait_ms: int = 20000) -> dict:
    """Load segments_map.html in a headless Chromium/Edge and count rendered layers, dashed paths and tiles."""
    import geopandas as gpd

    page = run.out / "segments_map.html"
    gdf = gpd.read_file(run.out / "segments.shp", encoding="UTF-8")
    expected = dict(parts=len(gdf), hypotheses=int((gdf.is_hypo == 1).sum()), segments=gdf.seg_id.nunique())
    text = page.read_text(encoding="utf-8")
    from .webmap import embedded_counts

    static = embedded_counts(text)
    res = dict(date=today(), file=str(page), expected=expected, static=static)
    exe = _browser()
    if not exe:
        res["browser"] = "none found (set SEGMAP_BROWSER); static checks only"
    else:
        prof = run.dir / "tmp" / "browser_profile"
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={prof}", f"--virtual-time-budget={wait_ms}",
               "--dump-dom", page.as_uri()]
        try:
            dom = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120).stdout
            res.update(browser=Path(exe).name, dom_bytes=len(dom),
                       leaflet_container="leaflet-container" in dom,
                       interactive_paths=len(re.findall(r'class="leaflet-interactive"', dom)),
                       dashed_paths=len(re.findall(r'stroke-dasharray="8 6"', dom)),
                       tiles_loaded=len(re.findall(r"leaflet-tile-loaded", dom)),
                       layer_control_entries=len(re.findall(r'class="leaflet-control-layers-selector"', dom)))
            res["loads"] = bool(res["leaflet_container"] and res["interactive_paths"] >= expected["parts"])
            res["dashed_ok"] = res["dashed_paths"] >= expected["hypotheses"]
            if res["tiles_loaded"] == 0:
                res["tiles_note"] = "No web base-map tile loaded (offline mode, offline machine or blocked); the map itself renders."
        except Exception as exc:
            res["browser_error"] = str(exc)
        finally:
            shutil.rmtree(prof, ignore_errors=True)
    write_json(run.out / "map_check.json", res)
    return res


# ============================================================================ validation


def validate(run: Run) -> dict:
    import geopandas as gpd

    errs, warns = [], []
    shp = run.out / "segments.shp"
    for ext in ("shp", "shx", "dbf", "prj", "cpg"):
        if not (run.out / f"segments.{ext}").exists():
            errs.append(f"missing segments.{ext}")
    if errs:
        return dict(errors=errs, warnings=warns)
    g = gpd.read_file(shp, encoding="UTF-8")
    ev = _load_evidence(run)
    if g.crs is None or g.crs.to_epsg() != run.epsg:
        errs.append(f"shapefile CRS {g.crs} is not {run.crs}")
    if not g.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        errs.append("non-polygon features")
    if not g.geometry.is_valid.all() or g.geometry.is_empty.any():
        errs.append("invalid or empty geometries")
    if any(len(c) > 10 for c in g.columns if c != "geometry"):
        errs.append("field name longer than 10 characters")
    if [c for c in g.columns if c != "geometry"] != SHP_FIELDS:
        errs.append(f"fields differ from required list: {list(g.columns)}")
    if not np.allclose(g.geometry.area / 1e4, g.area_ha.astype(float), atol=1e-4):
        errs.append("area_ha does not match geometry area")
    if not g.part_id.is_unique:
        errs.append("part_id not unique")
    if set(g.part_id) != {e["part_id"] for e in ev} or len(ev) != len(g):
        errs.append("evidence CSV does not join one-to-one with the shapefile by part_id")
    seg_ids = [s["seg_id"] for s in run.segments]
    missing = set(seg_ids) - set(g.seg_id.astype(str))
    if missing:
        errs.append(f"segments without polygon: {sorted(missing)}")
    if (run.out / "segments.cpg").read_text().strip().upper() not in ("UTF-8", "UTF8"):
        errs.append(".cpg is not UTF-8")
    hi = g[(g.confidence == "high") & (g.is_hypo != 0)]
    if len(hi):
        errs.append(f"high confidence with is_hypo 1: {list(hi.part_id)}")
    for s in run.segments:
        sub = g[g.seg_id.astype(str) == s["seg_id"]]
        for col, key in (("process", "process"), ("category", "category"), ("interact", "interact"), ("phase", "phase")):
            if len(sub) and not (sub[col].astype(str).str.rstrip() == _shp_text(s[key]).rstrip()).all():
                errs.append(f"segment {s['seg_id']}: {col} differs from the input table")
    for e in ev:
        if e["rule_5d"] == "1":
            if (e["placement"], e["confidence"], e["is_hypo"]) != ("modelled", "low", "1"):
                errs.append(f"{e['part_id']}: rule 5d classification")
            if len(_j(e["sensitivity_json"], [])) < 3:
                errs.append(f"{e['part_id']}: rule 5d sensitivity has fewer than 3 values")
        if not _j(e["quotations_json"], []) and not e["figure_refs"] and e["table_observations_json"] in ("", "[]"):
            errs.append(f"{e['part_id']}: no evidence")
    pdfs = [p for p in run.dir.rglob("*.pdf") if "tmp" not in p.parts]
    if pdfs:
        warns.append(f"report copies inside the run folder (rule: do not copy the report): {[str(p.relative_to(run.dir)) for p in pdfs]}")
    built = read_json(run.out / "checks.json", {})
    errs += [f"build: {x}" for x in built.get("errors", [])]
    return dict(errors=errs, warnings=warns + built.get("warnings", []), features=len(g), segments=int(g.seg_id.nunique()),
                hypotheses=int((g.is_hypo == 1).sum()))


# ============================================================================ README and report


def _sources_rows(run: Run):
    return [[s.get("name"), s.get("provider", ""), s.get("url", ""), s.get("layer", ""), s.get("date_or_version", ""),
             s.get("licence", "") + (f" ({s['licence_url']})" if s.get("licence_url") else ""), s.get("used_for", "")]
            for s in run.manifest() if s.get("kind") != "failed"]


def write_readme(run: Run) -> Path:
    import geopandas as gpd

    spec = run.spec()
    g = gpd.read_file(run.out / "segments.shp", encoding="UTF-8")
    ev = _load_evidence(run)
    lines = [f"# {spec.get('event') or run.config.get('event', '')} — reviewer package", "",
             f"{len(g)} polygon parts for {g.seg_id.nunique()} segments. CRS: {run.crs} ({_crs_name(run)}). Encoding UTF-8.",
             "Overlapping parts are intentional (successive processes); do not sum areas.", "",
             "## Contents", "",
             "- `segments.shp` (+ .shx .dbf .prj .cpg): one polygon feature per placed part.",
             "- `segments_evidence.csv`: full evidence per part, keyed by `part_id` (quotations with pages, visual observations, "
             "georeferencing control points and errors, reasoning, uncertainty, alternatives, classification reasons, fits, sensitivity).",
             "- `segments_map.html`: interactive map (needs internet for base maps and the Leaflet library). One layer per segment; "
             "dashed outline = is_hypo 1; click a polygon for all attributes and evidence.", "",
             "## Shapefile fields", "", table(["Field", "Meaning"], FIELD_HELP.items()),
             "Shapefile text is limited to 254 bytes; full texts are in the CSV.", "",
             "Classification (independent fields): `placement` = how the geometry was built; `is_hypo` = whether the reviewer must check "
             "the identity or the extent; `confidence` = confidence in the location, not in the outline.", "",
             "Selected CSV fields: `quotations` (verbatim report text, whitespace-normalised, with PDF and printed pages), "
             "`visual_observations_json` (analyst descriptions, never quotations; captions quoted separately), `georeferencing_json` "
             "(transformation, control-point pixel and map coordinates, residuals, independent checks), `fit_json` (area fits — not "
             "independent checks), `sensitivity_json` (parameter → area), `rule_5d` (1 = extent had no basis in the report).", "",
             "## Hypotheses to check", ""]
    hyp = [e for e in ev if e["is_hypo"] == "1"]
    lines.append(table(["Part", "Segment", "Label", "What to check / alternatives"],
                       [[e["part_id"], e["seg_id"], e["part_label"], e["alternative_hypotheses"]] for e in hyp]) if hyp else "None.\n")
    if spec.get("readme_notes"):
        lines += ["## Notes", "", spec["readme_notes"], ""]
    lines += ["## Data sources and licences", "", table(["Name", "Provider", "URL", "Layer", "Date/version", "Licence", "Use"], _sources_rows(run)),
              "The report itself, its images and the input segment table are not included."]
    target = run.out / "README.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _crs_name(run: Run) -> str:
    try:
        from pyproj import CRS

        return CRS.from_user_input(run.crs).name
    except Exception:
        return ""


def write_report(run: Run, validation: dict, map_check: dict | None) -> Path:
    import geopandas as gpd

    spec = run.spec()
    notes = spec.get("report_notes", {})
    g = gpd.read_file(run.out / "segments.shp", encoding="UTF-8")
    ev = _load_evidence(run)
    checks = read_json(run.out / "checks.json", {})
    visuals = read_json(run.raw / "visual_evidence.json", [])
    georefs = read_json(run.raw / "georeferencing.json", {})
    N = lambda k: [notes[k], ""] if notes.get(k) else []
    L = [f"# {spec.get('event') or run.config.get('event', '')} — GIS placement report", "",
         f"Report: `{run.report_path.name}` ({len(run.pages())} PDF pages; printed page = PDF page + {run.page_offset}). "
         f"CRS: {run.crs} {_crs_name(run)}. Built {today()} with segment_mapping {__version__}.", "",
         f"**{len(g)} polygon parts cover {g.seg_id.nunique()} of {len(run.segments)} segments; {int((g.is_hypo == 1).sum())} parts are hypotheses (is_hypo = 1).** "
         "Overlaps between parts are intentional.", ""]
    if run.config.get("mode") == "text":
        L += ["**Text-only run:** placements use the report text and provided spatial data only. Figures, maps and photographs were "
              "not inspected (known only from captions), so no report outline was digitized and no part is `documented`.", ""]
    L += N("summary")

    inv = run.raw / "inventory.md"
    L += ["## Spatial data before placement (rule 2)", "", inv.read_text(encoding="utf-8").split("\n", 1)[-1].strip() if inv.exists() else "Inventory not generated.", ""]
    L += ["## Data found and used", "", table(["Name", "Provider", "URL", "Layer", "Date / version", "Licence", "Use"], _sources_rows(run))] + N("data")
    failed = [s for s in run.manifest() if s.get("kind") == "failed"]
    gaps = spec.get("data_gaps", [])
    L += ["## Data searched for but not found or not accessible", ""]
    if failed:
        L += [table(["Dataset", "URL", "Error"], [[s["name"], s.get("url", ""), s.get("error", "")[:200]] for s in failed])]
    L += [table(["Searched for", "Result", "Effect on placements"], [[d.get("searched", ""), d.get("result", ""), d.get("effect", "")] for d in gaps])
          if gaps else "No gaps recorded.\n"] + N("gaps")

    L += ["## Placements per segment", "",
          "`placement` = geometry construction; `is_hypo` = reviewer check of identity or unsupported extent; `confidence` = location confidence.", ""]
    for s in run.segments:
        sel = [e for e in ev if e["seg_id"] == s["seg_id"]]
        L += [f"### Segment {s['seg_id']}: {s['process'].strip()}", "",
              table(["Part", "Meaning", "Placement", "Confidence", "Hypothesis", "Area ha", "Main evidence"],
                    [[e["part_id"], e["part_label"], e["placement"], e["confidence"], "yes" if e["is_hypo"] == "1" else "no",
                      f"{float(e['area_ha']):.4f}", f"printed p. {e['rep_page']}" + (f"; fig. {e['figure_refs']}" if e["figure_refs"] else "")
                      + f" ({e['evidence_type']})"] for e in sel])]
        for e in sel:
            L += [f"- **{e['part_id']}** — {e['reasoning']} *Uncertainty:* {e['uncertainty']}"
                  + (f" *Alternatives:* {e['alternative_hypotheses']}" if e["alternative_hypotheses"] else "")
                  + f" *Classification:* placement — {e['reason_placement']}; is_hypo — {e['reason_is_hypo']}; confidence — {e['reason_confidence']}"]
        L.append("")

    L += ["## Checks", ""]
    for name, c in (checks.get("chainages") or {}).items():
        L += [f"**Chainage `{name}`** (unit {c['unit_m']:g} m; GIS line {c['line_length_m']} m = {c['line_length_units']} units). {c['method']}.", "",
              table(["Anchor", "Report value", "GIS distance m", "Offset GIS − report m"],
                    [[a.get("label", ""), a["value"], a["raw_m"], a["offset_m"]] for a in c["anchors"]])]
        if c.get("scales"):
            L += ["Scale factors between anchors: " + "; ".join(f"{s['between'][0]}–{s['between'][1]}: {s['scale']}" for s in c["scales"]), ""]
    gc = checks.get("checks", [])
    if gc:
        L += [table(["Check", "Report", "GIS", "Difference", "Kind", "Note"],
                    [[c["label"], f"{_blank(c.get('report_value'))} {c.get('report_text', '')}".strip() + (f" (p. {c['page']})" if c.get("page") else ""),
                      f"{c['value']} {c['unit']}", f"{c.get('difference', '')}" + (f" ({c['difference_pct']}%)" if c.get("difference_pct") is not None else ""),
                      c.get("kind", ""), c.get("note", "")] for c in gc])]
    pc = [(e["part_id"], c) for e in ev for c in _j(e["checks_json"], [])]
    if pc:
        L += ["Part-level checks:", "", table(["Part", "Check", "Report", "GIS", "Difference"],
                                              [[p, c["label"], f"{_blank(c.get('report_value'))} {c.get('report_text', '')}".strip(), f"{c['value']} {c['unit']}",
                                                f"{c.get('difference', '')}" + (f" ({c['difference_pct']}%)" if c.get("difference_pct") is not None else "")] for p, c in pc])]
    fits = [(e["part_id"], k, f) for e in ev for k, f in _j(e["fit_json"], {}).items()]
    if fits:
        L += ["Area fits (rule 5b) — fitted values are **not** independent checks:", "",
              table(["Part", "Fitted unit", "Target ha", "Fitted ha", "Error %", "Parameter", "Value"],
                    [[p, k, f["target_ha"], f["fitted_ha"], f["error_pct"], f"{f.get('parameter_name', '')} {f.get('unit', '')}", f["parameter_value"]] for p, k, f in fits])]
    L += N("checks")

    L += ["## Visual evidence used", ""]
    if visuals:
        L += [table(["Figure", "PDF / printed p.", "Type", "Caption (quoted)", "What it shows (observation)", "Date / event relevance", "Use / method"],
                    [[v.get("figure"), f"{v.get('pdf_page', '')} / {v.get('printed_page', '')}", v.get("evidence_type", ""), v.get("caption_quotation", ""),
                      v.get("observation", ""), v.get("date_relevance", ""), v.get("method", "")] for v in visuals])]
    if georefs:
        L += ["Georeferencing (pixel origin upper-left, x right, y down; full control points in `georeferencing_json` of the CSV):", "",
              table(["Id", "Figure", "Source → target CRS", "Transform", "GCPs", "RMS m", "Max residual m", "Independent checks (error m)", "Pixel m", "Limitations"],
                    [[k, gr.get("figure"), f"{gr.get('source_crs')} → {gr.get('target_crs')}", gr["transformation"], len(gr["control_points"]), gr["rms_m"],
                      gr["max_residual_m"], ", ".join(f"{c.get('label', '')}: {c['error_m']}" for c in gr["independent_checks"]) or "none (no independent validation)",
                      gr["ground_pixel_size_m"], (gr.get("limitations", "") + " " + gr.get("note", "")).strip()] for k, gr in georefs.items()])]
    L += N("visual")

    sens = [e for e in ev if _j(e["sensitivity_json"], [])]
    L += ["## Sensitivity", ""]
    if sens:
        L += [table(["Part", "Rule 5d", "Parameter", "Chosen", "Values → area ha"],
                    [[e["part_id"], "yes" if e["rule_5d"] == "1" else "no", f"{_j(e['parameter_json'], {}).get('name', '')} ({_j(e['parameter_json'], {}).get('unit', '')})",
                      _j(e["parameter_json"], {}).get("value", next(iter(_j(e["fit_json"], {}).values()), {}).get("parameter_value", "")),
                      "; ".join(f"{s['value']} → {s['area_ha']}" for s in _j(e["sensitivity_json"], []))] for e in sens])]
    else:
        L += ["No parameterised parts.", ""]
    L += N("sensitivity")

    L += ["## Uncertain interpretations and alternatives", ""]
    L += [f"- **{e['part_id']}** ({e['part_label']}): {e['alternative_hypotheses']}" for e in ev if e["is_hypo"] == "1"] + [""]
    L += N("uncertain")

    L += ["## Validation", "", f"Errors: {len(validation['errors'])}. Warnings: {len(validation['warnings'])}.", ""]
    L += [f"- ERROR: {x}" for x in validation["errors"]] + [f"- warning: {x}" for x in validation["warnings"]] + [""]
    if map_check:
        L += ["Map load check: " + json.dumps({k: v for k, v in map_check.items() if k not in ("file",)}, ensure_ascii=False), ""]
    L += N("other")
    L += ["## Environment and reproduction", "", f"Python {sys.version.split()[0]}; packages: {_packages()}.", "",
          "Commands run (scripts/commands.log); all placement decisions are in `scripts/placements.json`:", "", "```"]
    log = run.dir / "scripts" / "commands.log"
    L += [log.read_text(encoding="utf-8").strip() if log.exists() else "(no log)", "```", ""]
    target = run.out / "data_report.md"
    target.write_text("\n".join(L) + "\n", encoding="utf-8")
    return target


def _packages() -> str:
    from importlib.metadata import PackageNotFoundError, version

    out = []
    for p in ("geopandas", "pyogrio", "shapely", "pyproj", "rasterio", "numpy", "scipy", "pypdf", "pdfplumber", "pypdfium2", "matplotlib", "requests"):
        try:
            out.append(f"{p} {version(p)}")
        except PackageNotFoundError:
            pass
    return ", ".join(out)


def make_zip(run: Run) -> Path:
    name = f"{_slug(run.spec().get('event') or run.config.get('event', 'event'))}_review.zip"
    target = run.out / name
    for old in run.out.glob("*_review.zip"):
        old.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ZIP_FILES:
            z.write(run.out / f, arcname=f)
    with zipfile.ZipFile(target) as z:
        assert sorted(z.namelist()) == sorted(ZIP_FILES) and z.testzip() is None
        for f in ZIP_FILES:
            assert hashlib.sha256(z.read(f)).digest() == hashlib.sha256((run.out / f).read_bytes()).digest()
    return target


def deliver(run: Run, browser_check: bool = True) -> dict:
    make_map(run)
    write_readme(run)
    mc = check_map(run) if browser_check else None
    v = validate(run)
    if mc and mc.get("browser") and not str(mc.get("browser")).startswith("none"):
        if not mc.get("loads"):
            v["errors"].append(f"map did not load correctly in the headless browser: {mc}")
        elif not mc.get("dashed_ok"):
            v["errors"].append("map: fewer dashed paths than hypothesis parts")
    report = write_report(run, v, mc)
    z = make_zip(run)
    return dict(validation=v, map_check=mc, report=str(report), zip=str(z))
