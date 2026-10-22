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
from .build import SHP_FIELDS, OUTCOME_FIELDS, GEOMETRY_ROLES, OUTCOME_CONFIDENCE, segment_outcomes, _shp_text
from .geom import Evaluator
from .runctx import Run, read_json, today, write_json

ZIP_FILES = ["segments.shp", "segments.shx", "segments.dbf", "segments.prj", "segments.cpg", "segments_evidence.csv",
             "segments_map.html", "README.md", "segments.geojson", "segment_results.json", "segment_results.csv", "data_report.md", "checks.json"]
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


def load_geometries(run: Run):
    """Read the canonical mixed geometries, including an empty unresolved run."""
    import geopandas as gpd
    path = run.out / "segments.geojson"
    if path.exists():
        data = read_json(path, {})
        if not data.get("features"):
            return gpd.GeoDataFrame(columns=SHP_FIELDS + OUTCOME_FIELDS + ["geometry"], geometry="geometry", crs=4326)
        return gpd.GeoDataFrame.from_features(data["features"], crs=4326)
    return gpd.read_file(run.out / "segments.shp", encoding="UTF-8")


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
    rows = "".join(f"<tr><th>{esc(k)}</th><td>{esc(str(e.get(k, '')))}</td></tr>" for k in SHP_FIELDS + OUTCOME_FIELDS)
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
    gdf = load_geometries(run)
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
    canonical = run.out / "segments.geojson"
    shp = run.out / "segments.shp"
    if not canonical.exists() and not shp.exists():
        return dict(errors=["missing segments.geojson or segments.shp"], warnings=[])
    g = load_geometries(run)
    ev = _load_evidence(run)
    expected_crs = 4326 if canonical.exists() else run.epsg
    if g.crs is None or g.crs.to_epsg() != expected_crs:
        errs.append(f"geometry CRS {g.crs} is not EPSG:{expected_crs}")
    if not g.geom_type.isin(["Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"]).all():
        errs.append("unsupported geometry types")
    if not g.geometry.is_valid.all() or g.geometry.is_empty.any():
        errs.append("invalid or empty geometries")
    if not g.part_id.is_unique:
        errs.append("part_id not unique")
    if set(g.part_id) != {e["part_id"] for e in ev} or len(ev) != len(g):
        errs.append("evidence CSV does not join one-to-one with canonical geometries by part_id")
    if len(g):
        projected = g.to_crs(run.crs)
        polys = projected.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        if not np.allclose(projected.loc[polys].geometry.area / 1e4, projected.loc[polys, "area_ha"].astype(float), atol=1e-4):
            errs.append("area_ha does not match geometry area")
        if ((g.confidence == "high") & (g.is_hypo.astype(int) != 0)).any():
            errs.append("high confidence with is_hypo 1")
    outcomes, issues = segment_outcomes(run.segments, run.spec(), ev)
    errs += issues
    stored = read_json(run.out / "segment_results.json", None)
    if canonical.exists() and stored != outcomes:
        errs.append("segment_results.json differs from validated localization outcomes")
    for e in ev:
        if e.get("geometry_role", "event_extent") not in GEOMETRY_ROLES:
            errs.append(f"{e['part_id']}: invalid geometry_role")
        for field in ("event_confidence", "location_confidence", "boundary_confidence"):
            if field in e and e[field] not in OUTCOME_CONFIDENCE:
                errs.append(f"{e['part_id']}: invalid {field}")
    for _, feature in g.iterrows():
        pid = feature["part_id"]
        role = feature.get("geometry_role", "event_extent" if feature.geometry.geom_type in ("Polygon", "MultiPolygon") else "location")
        boundary = feature.get("boundary_confidence", "unknown")
        if role not in GEOMETRY_ROLES:
            errs.append(f"{pid}: invalid geometry_role in canonical geometries")
        if role == "event_extent" and feature.geometry.geom_type not in ("Polygon", "MultiPolygon"):
            errs.append(f"{pid}: event_extent requires a polygon")
        if role != "event_extent" and boundary != "unknown":
            errs.append(f"{pid}: location/context/search_area requires unknown boundary_confidence")
        if boundary == "high" and feature["placement"] != "documented":
            errs.append(f"{pid}: high boundary_confidence requires a documented outline")
        if feature.get("localization_status", "located") != "located":
            errs.append(f"{pid}: a geometry must have located localization_status")
        for field in ("event_confidence", "location_confidence", "boundary_confidence"):
            if field in feature and feature[field] not in OUTCOME_CONFIDENCE:
                errs.append(f"{pid}: invalid {field} in canonical geometries")
        evidence = next((e for e in ev if e["part_id"] == pid), None)
        if evidence:
            if evidence.get("rule_5d") == "1" and boundary not in ("low", "unknown"):
                errs.append(f"{pid}: assumed extent requires low or unknown boundary_confidence")
            for field in OUTCOME_FIELDS:
                if field in feature and field in evidence and str(feature[field]) != evidence[field]:
                    errs.append(f"{pid}: {field} differs between canonical geometry and evidence CSV")
            if "geometry_type" in feature and feature["geometry_type"] != feature.geometry.geom_type:
                errs.append(f"{pid}: geometry_type differs from actual geometry")
    if shp.exists():
        for ext in ("shx", "dbf", "prj", "cpg"):
            if not (run.out / f"segments.{ext}").exists():
                errs.append(f"missing segments.{ext}")
        legacy = gpd.read_file(shp, encoding="UTF-8")
        if not legacy.geom_type.isin(["Polygon", "MultiPolygon"]).all():
            errs.append("non-polygon features in compatibility shapefile")
        if legacy.crs is None or legacy.crs.to_epsg() != run.epsg:
            errs.append("shapefile CRS differs from run CRS")
        if [c for c in legacy.columns if c != "geometry"] != SHP_FIELDS:
            errs.append("shapefile fields differ from required list")
        polygon_ids = set(g.loc[g.geom_type.isin(["Polygon", "MultiPolygon"]), "part_id"])
        if set(legacy.part_id) != polygon_ids:
            errs.append("shapefile does not match polygon subset of canonical geometries")
    for seg in run.segments:
        sub = g[g.seg_id.astype(str) == str(seg["seg_id"])]
        for col in ("process", "category", "interact", "phase"):
            if len(sub) and not sub[col].astype(str).str.rstrip().isin([str(seg[col]).rstrip(), _shp_text(seg[col]).rstrip()]).all():
                errs.append(f"segment {seg['seg_id']}: {col} differs from the input table")
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
    g = load_geometries(run)
    ev = _load_evidence(run)
    lines = [f"# {spec.get('event') or run.config.get('event', '')} — reviewer package", "",
             f"{len(g)} localized parts for {g.seg_id.nunique()} of {len(run.segments)} input segments. Canonical GeoJSON CRS: EPSG:4326. Encoding UTF-8.",
             "Overlapping parts are intentional (successive processes); do not sum areas.", "",
             "## Contents", "",
             "- `segments.geojson`: canonical mixed point, line and polygon geometries (EPSG:4326).",
             "- `segments.shp` (+ .shx .dbf .prj .cpg), when present: polygon subset only, in the run CRS.",
             "- `segment_results.json` / `segment_results.csv`: every input segment, status, reason and independent confidence fields; unresolved segments have no geometry.",
             "- `segments_evidence.csv`: full evidence per part, keyed by `part_id` (quotations with pages, visual observations, "
             "georeferencing control points and errors, reasoning, uncertainty, alternatives, classification reasons, fits, sensitivity).",
             "- `segments_map.html`: interactive map with Leaflet inlined; internet is optional for base maps. One layer per segment; "
             "dashed outline = is_hypo 1; click a feature for all attributes and evidence.", "",
             "Geometry roles distinguish event extents, locations, context and search areas. Event, location and boundary confidence are independent; unknown boundary confidence does not establish an event outline.", "",
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
    g = load_geometries(run)
    ev = _load_evidence(run)
    checks = read_json(run.out / "checks.json", {})
    visuals = read_json(run.raw / "visual_evidence.json", [])
    georefs = read_json(run.raw / "georeferencing.json", {})
    N = lambda k: [notes[k], ""] if notes.get(k) else []
    page_description = (f"{len(run.pages())} imported text-page records; evidence PDF page numbers are localization IDs, "
                        "which can differ from source-file pages. Visual source-file pages are listed under Use / method"
                        if run.config.get("pipeline_run") else
                        f"{len(run.pages())} PDF pages; printed page = PDF page + {run.page_offset}")
    L = [f"# {spec.get('event') or run.config.get('event', '')} — GIS placement report", "",
         f"Report: `{run.report_path.name}` ({page_description}). "
         f"CRS: {run.crs} {_crs_name(run)}. Built {today()} with segment_mapping {__version__}.", "",
         f"**{len(g)} localized parts cover {g.seg_id.nunique()} of {len(run.segments)} segments; {int((g.is_hypo == 1).sum())} parts are hypotheses (is_hypo = 1).** "
         "Overlaps between parts are intentional.", ""]
    if run.config.get("mode") == "text":
        L += ["**Text-only run:** placements use the report text and provided spatial data only. Figures, maps and photographs were "
              "not inspected (known only from captions), so no report outline was digitized and no part is `documented`.", ""]
    outcomes = read_json(run.out / "segment_results.json", [])
    tiers = {t: sum(r.get("precision") == t for r in outcomes) for t in ("specific", "approximate", "broad", "unresolved")}
    L += ["## Localization outcomes", "", "Precision (derived from the methods used): " + ", ".join(f"{n} {t}" for t, n in tiers.items()) + ".", "",
          table(["Segment", "Status", "Precision", "Reason", "Event confidence", "Location confidence", "Boundary confidence"],
          [[r.get(k, "") for k in ("seg_id", "localization_status", "precision", "reason", "event_confidence", "location_confidence", "boundary_confidence")] for r in outcomes]),
          "Context and search areas are reference geometries; a location does not assert an event boundary. Unknown boundary confidence means the extent is unsupported. "
          "Precision: specific = identified feature or reach between stated points; approximate = valley floor, elevation band or whole stream; "
          "broad = whole catchment or search area (appropriate only for catchment-wide conditions and triggers).", ""]
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
                      f"{float(e['area_ha']):.4f}" if e["area_ha"] else "n/a", f"printed p. {e['rep_page']}" + (f"; fig. {e['figure_refs']}" if e["figure_refs"] else "")
                      + f" ({e['evidence_type']})"] for e in sel])]
        for e in sel:
            L += [f"- Geometry: {e.get('geometry_type', '')}; role: {e.get('geometry_role', 'event_extent')}; boundary confidence: {e.get('boundary_confidence', 'unknown')}."]
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
    files = [f for f in ZIP_FILES if (run.out / f).exists()]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(run.out / f, arcname=f)
    with zipfile.ZipFile(target) as z:
        assert sorted(z.namelist()) == sorted(files) and z.testzip() is None
        for f in files:
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
