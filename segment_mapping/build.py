"""Build placements from scripts/placements.json: shapefile, evidence CSV, checks."""
from __future__ import annotations

import csv
import json
import math


from .geom import Evaluator, SpecError, polygons_only
from .report import caption_for, resolve_quote
from .runctx import Run, write_json

PLACEMENTS = ("documented", "approximate", "modelled", "hypothesis")
CONFIDENCE = ("high", "medium", "low")
EVIDENCE_TYPES = ("text", "table", "map", "photograph", "orthophoto", "diagram", "mixed")
SHP_FIELDS = ["seg_id", "part_id", "part_label", "process", "category", "interact", "phase", "placement", "is_hypo",
              "confidence", "geom_basis", "data_src", "area_ha", "rep_value", "rep_page"]
EVIDENCE_FIELDS = SHP_FIELDS + [
    "pdf_pages", "evidence_type", "quotations", "quotations_json", "figure_refs", "visual_observations_json",
    "table_observations_json", "image_date_relevance", "visual_identification_method", "georeferencing_json",
    "georeferencing_limitations", "reasoning", "uncertainty", "alternative_hypotheses", "reason_placement",
    "reason_is_hypo", "reason_confidence", "rule_5d", "parameter_json", "fit_json", "fit_is_independent_check",
    "sensitivity_json", "checks_json"]


# ============================================================================ parameters, fits, sensitivity


def _has_param(expr) -> bool:
    return "$param" in json.dumps(expr)


def evaluate_parameter(ev: Evaluator, expr, parameter: dict | None, label: str = ""):
    """Return (geometry, fit_info|None). parameter: {"value": v} or {"fit_area_ha": T, "bounds": [lo, hi]}."""
    if not parameter:
        if _has_param(expr):
            raise SpecError(f"{label}: expression uses $param but no parameter is given")
        return ev.geom(expr), None
    if "fit_area_ha" in parameter:
        target = float(parameter["fit_area_ha"])
        lo, hi = [float(v) for v in parameter.get("bounds", [0, 100])]
        tol = float(parameter.get("tolerance_pct", 5))

        def area(v):
            return polygons_only(ev.geom(expr, v)).area / 1e4

        a_lo, a_hi = area(lo), area(hi)
        increasing = a_hi >= a_lo
        if not (min(a_lo, a_hi) <= target <= max(a_lo, a_hi)):
            raise SpecError(f"{label}: target {target} ha outside the range reachable within bounds {lo}–{hi} "
                            f"({a_lo:.4f}–{a_hi:.4f} ha); widen 'bounds' or change the method")
        best = None
        for it in range(40):
            mid = (lo + hi) / 2
            a = area(mid)
            if best is None or abs(a - target) < abs(best[1] - target):
                best = (mid, a)
            if abs(a - target) / target * 100 < tol / 20:
                break
            if (a < target) == increasing:
                lo = mid
            else:
                hi = mid
        value, fitted = best
        err = 100 * (fitted - target) / target
        fit = dict(target_ha=target, fitted_ha=round(fitted, 6), parameter_value=round(value, 6), error_pct=round(err, 3),
                   tolerance_pct=tol, within_tolerance=abs(err) <= tol, search_bounds=parameter.get("bounds", [0, 100]),
                   iterations=it + 1, parameter_name=parameter.get("name", ""), unit=parameter.get("unit", ""),
                   independent_check=False, note="Fitted to the report value; agreement is by construction, not validation.")
        if abs(err) > tol:
            raise SpecError(f"{label}: best fit {fitted:.4f} ha differs {err:.2f}% from target {target} ha (> {tol}%)")
        return ev.geom(expr, value), fit
    if "value" in parameter:
        return ev.geom(expr, parameter["value"]), None
    raise SpecError(f"{label}: parameter needs 'value' or 'fit_area_ha'")


def sensitivity(ev: Evaluator, expr, values) -> list[dict]:
    out = []
    for v in values:
        g = polygons_only(ev.geom(expr, v))
        out.append(dict(value=v, area_ha=round(g.area / 1e4, 6)))
    return out


# ============================================================================ evidence helpers


def _shp_text(v, limit=254) -> str:
    s = "" if v is None else str(v)
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[: limit - 3].decode("utf-8", errors="ignore") + "…"


def _jsonable(v):
    return json.dumps(v, ensure_ascii=False, default=str)


def visual_ledger(run: Run, spec: dict) -> list[dict]:
    out = []
    for v in spec.get("visuals", []):
        rec = dict(v)
        fig = str(v.get("figure"))
        rec["figure"] = fig
        if v.get("pdf_page") and not v.get("printed_page"):
            rec["printed_page"] = run.printed(int(v["pdf_page"]))
        if v.get("printed_page") and not v.get("pdf_page"):
            rec["pdf_page"] = run.pdf_page(int(v["printed_page"]))
        if v.get("caption"):
            q = resolve_quote(run, dict(text=v["caption"], pdf_page=rec.get("pdf_page")))
            rec["caption_quotation"], rec["caption_verified"] = q["text"], q["verified"]
        else:
            c = caption_for(run, fig, v.get("kind"))
            rec["caption_quotation"] = c["caption"] if c else ""
            rec["caption_verified"] = bool(c)
            if c and not rec.get("pdf_page"):
                rec["pdf_page"], rec["printed_page"] = c["pdf_page"], c["printed_page"]
        rec.pop("caption", None)
        rec["observation_is_verbatim"] = False
        out.append(rec)
    return out


def table_ledger(run: Run, spec: dict) -> list[dict]:
    out = []
    for t in spec.get("tables", []):
        rec = dict(t)
        rec["table"] = str(t.get("table"))
        if t.get("printed_page") and not t.get("pdf_page"):
            rec["pdf_page"] = run.pdf_page(int(t["printed_page"]))
        if t.get("pdf_page") and not t.get("printed_page"):
            rec["printed_page"] = run.printed(int(t["pdf_page"]))
        rec["observation_is_verbatim"] = False
        out.append(rec)
    return out


# ============================================================================ validation


def classify_issues(part: dict, built: dict, visuals: dict, tables: dict, georefs: dict,
                    text_mode: bool = False) -> tuple[list[str], list[str]]:
    errors, warns = [], []
    pid = part.get("part_id", "?")
    E = lambda m: errors.append(f"{pid}: {m}")
    W = lambda m: warns.append(f"{pid}: {m}")
    pl, conf, hyp = part.get("placement"), part.get("confidence"), part.get("is_hypo")
    if pl not in PLACEMENTS:
        E(f"placement must be one of {PLACEMENTS}")
    if conf not in CONFIDENCE:
        E(f"confidence must be one of {CONFIDENCE}")
    if hyp not in (0, 1):
        E("is_hypo must be 0 or 1")
    if conf == "high" and hyp != 0:
        E("confidence high requires is_hypo 0 (rule 8)")
    if part.get("evidence_type") not in EVIDENCE_TYPES:
        E(f"evidence_type must be one of {EVIDENCE_TYPES}")
    for k in ("part_label", "geom_basis", "data_src", "reasoning", "uncertainty", "reason_placement", "reason_is_hypo", "reason_confidence"):
        if not str(part.get(k, "")).strip():
            E(f"'{k}' is required")
    if hyp == 1 and not str(part.get("alternatives", "")).strip():
        E("is_hypo 1 requires 'alternatives' (rule 6)")
    if not part.get("rep_page"):
        W("rep_page is empty")
    if part.get("rule_5d"):
        if (pl, conf, hyp) != ("modelled", "low", 1):
            E("rule_5d parts must be placement modelled, confidence low, is_hypo 1")
        sens = built.get("sensitivity") or []
        chosen = (part.get("parameter") or {}).get("value")
        if len(sens) < 3 or chosen is None or not any(s["value"] < chosen for s in sens) or not any(s["value"] > chosen for s in sens):
            E("rule_5d needs parameter.value and sensitivity values both lower and higher than it")
        if (part.get("parameter") or {}).get("fit_area_ha"):
            E("rule_5d applies when the report gives no extent constraint; a fitted area contradicts that")
    if text_mode and pl == "documented":
        E("text-only run: no figure was inspected, so no outline is documented; use approximate/modelled/hypothesis")
    if pl == "documented" and not (part.get("georef") or part.get("figures")):
        W("documented placement without a georef/figure reference: is the outline really explicit in the report?")
    if pl == "documented" and (part.get("parameter") or {}).get("fit_area_ha"):
        E("a documented outline must not be fitted to an area (rule 5b)")
    quotes = built.get("quotes", [])
    for q in quotes:
        if not q.get("verified"):
            E(f"quotation not found in report text: {q['text'][:80]!r}")
        elif not q.get("page_hint_ok", True):
            W(f"quotation found on PDF page {q['pdf_page']}, not on the page given: {q['text'][:60]!r}")
    if not quotes and not part.get("figures") and not part.get("tables"):
        E("no evidence: give quotes and/or figures/tables")
    for f in part.get("figures", []):
        if str(f) not in visuals:
            E(f"figure {f} is not in the 'visuals' ledger")
    for t in part.get("tables", []):
        if str(t) not in tables:
            E(f"table {t} is not in the 'tables' ledger")
    for gid in part.get("georef", []):
        if gid not in georefs:
            E(f"georef {gid} not defined")
    fit = built.get("fit")
    if fit and not fit.get("within_tolerance", True):
        E(f"area fit outside tolerance ({fit['error_pct']}%)")
    return errors, warns


# ============================================================================ spec structure

KNOWN_KEYS = {
    "top": {"event", "refs", "chainages", "georef", "visuals", "tables", "parts", "checks", "data_gaps", "report_notes", "readme_notes", "map"},
    "part": {"part_id", "seg_id", "part_label", "geometry", "parameter", "placement", "is_hypo", "confidence", "rule_5d", "evidence_type",
             "figures", "georef", "tables", "quotes", "visual_notes", "image_date_relevance", "visual_identification_method",
             "georeferencing_limitations", "geom_basis", "data_src", "rep_value", "rep_page", "reasoning", "uncertainty", "alternatives",
             "reason_placement", "reason_is_hypo", "reason_confidence", "fit_from", "checks", "comment", "method", "method_args"},
    "parameter": {"name", "unit", "value", "fit_area_ha", "bounds", "tolerance_pct", "sensitivity"},
    "check": {"label", "measure", "report_value", "report_text", "page", "kind", "note"},
    "data_gap": {"searched", "result", "effect"},
    "visual": {"figure", "kind", "pdf_page", "printed_page", "evidence_type", "caption", "observation", "date_relevance", "method"},
    "table": {"table", "pdf_page", "printed_page", "observation"},
    "report_notes": {"summary", "data", "gaps", "checks", "visual", "sensitivity", "uncertain", "other"},
    "georef": {"figure", "image", "pdf_page", "printed_page", "method", "source_crs", "target_crs", "gcps", "checks", "limitations"},
}


def spec_key_issues(spec: dict) -> list[str]:
    """Unknown keys are silently ignored by the toolkit, so report them (typos, undocumented names)."""
    out = []

    def chk(obj, kind, where):
        if isinstance(obj, dict):
            bad = sorted(set(obj) - KNOWN_KEYS[kind])
            if bad:
                out.append(f"{where}: unknown key(s) {bad} are ignored; allowed: {sorted(KNOWN_KEYS[kind])}")

    chk(spec, "top", "spec")
    for p in spec.get("parts", []):
        chk(p, "part", f"part {p.get('part_id', '?')}")
        chk(p.get("parameter"), "parameter", f"part {p.get('part_id', '?')} parameter")
        for c in p.get("checks", []):
            chk(c, "check", f"part {p.get('part_id', '?')} check")
    for c in spec.get("checks", []):
        chk(c, "check", f"check {c.get('label', '')!r}")
    for d in spec.get("data_gaps", []):
        chk(d, "data_gap", "data_gaps entry")
    for v in spec.get("visuals", []):
        chk(v, "visual", f"visual {v.get('figure', '?')}")
    for t in spec.get("tables", []):
        chk(t, "table", f"table {t.get('table', '?')}")
    chk(spec.get("report_notes"), "report_notes", "report_notes")
    for gid, g in (spec.get("georef") or {}).items():
        chk(g, "georef", f"georef {gid}")
    return out


def fixed_buffer_issue(part: dict) -> str | None:
    """Rule 5c/5d: a plain fixed-width buffer is a last resort; flag it when no sensitivity is given."""
    g = part.get("geometry")
    tops = g.get("union", [g]) if isinstance(g, dict) else []
    for t in tops:
        if isinstance(t, dict) and "buffer" in t:
            b = t["buffer"]
            w = b.get("width", b.get("distance"))
            if isinstance(w, (int, float)) and w >= 1 and not (part.get("parameter") or {}).get("sensitivity"):
                return (f"{part.get('part_id')}: fixed-width buffer ({w} m) without sensitivity — rule 5c says last resort: "
                        "justify the width in geom_basis/reasoning, or make it a parameter with sensitivity (rule 5d if the report gives no extent)")
    return None


# ============================================================================ build


def build(run: Run, only: list[str] | None = None, write: bool = True) -> dict:
    import geopandas as gpd

    spec = run.spec()
    ev = Evaluator(run, spec)
    seg_ids = [s["seg_id"] for s in run.segments]
    int_ids = all(s.isdigit() for s in seg_ids)
    visuals = {v["figure"]: v for v in visual_ledger(run, spec)}
    tables = {t["table"]: t for t in table_ledger(run, spec)}
    errors, warns = [], spec_key_issues(spec)
    warns += [w for w in (fixed_buffer_issue(p) for p in spec.get("parts", [])) if w]

    # ---------------------------------------------------------------- georeferencing
    georefs = {}
    for gid in spec.get("georef", {}):
        try:
            georefs[gid] = ev.georef(gid)
        except Exception as exc:
            errors.append(f"georef {gid}: {exc}")
    for gid, gr in georefs.items():
        if not gr.checks:
            warns.append(f"georef {gid}: no independent check point (residuals at fitted GCPs are not validation)")

    parts = spec.get("parts", [])
    if only:
        parts = [p for p in parts if p.get("part_id") in only or str(p.get("seg_id")) in only]
    seen, features, evidence = set(), [], []
    for part in parts:
        pid = str(part.get("part_id", ""))
        sid = str(part.get("seg_id", ""))
        if not pid:
            errors.append("part without part_id")
            continue
        if pid in seen:
            errors.append(f"{pid}: duplicate part_id")
        seen.add(pid)
        try:
            seg = run.segment(sid)
        except KeyError as exc:
            errors.append(f"{pid}: {exc}")
            continue
        built: dict = {}
        try:
            g, fit = evaluate_parameter(ev, part["geometry"], part.get("parameter"), label=pid)
            g = polygons_only(g)
            if g.is_empty:
                raise SpecError("geometry is empty after evaluation (check limits/clips)")
            built["fit"] = fit
            par = part.get("parameter") or {}
            vals = list(par.get("sensitivity", []))
            if vals:
                chosen = fit["parameter_value"] if fit else par.get("value")
                if chosen is not None and all(abs(v - chosen) > 1e-9 for v in vals):
                    vals.append(chosen)
                built["sensitivity"] = sensitivity(ev, part["geometry"], sorted(vals))
        except Exception as exc:
            errors.append(f"{pid}: geometry failed: {exc}")
            continue
        built["quotes"] = [resolve_quote(run, q) for q in part.get("quotes", [])]
        e1, w1 = classify_issues(part, built, visuals, tables, georefs, run.config.get("mode") == "text")
        errors += e1
        warns += w1
        checks = []
        for c in part.get("checks", []):
            try:
                checks.append(_check(ev, c))
            except Exception as exc:
                errors.append(f"{pid}: check {c.get('label', '')}: {exc}")
        fit_from = {r: ev.ref_fits[r] for r in part.get("fit_from", []) if r in ev.ref_fits}
        for r in part.get("fit_from", []):
            if r not in ev.ref_fits:
                ev.ref(r)
                if r in ev.ref_fits:
                    fit_from[r] = ev.ref_fits[r]
                else:
                    errors.append(f"{pid}: fit_from {r!r} is not a fitted ref")
        area = g.area / 1e4
        pages_pdf = sorted({q["pdf_page"] for q in built["quotes"] if q.get("pdf_page")} |
                           {int(visuals[str(f)]["pdf_page"]) for f in part.get("figures", []) if str(f) in visuals and visuals[str(f)].get("pdf_page")})
        attrs = dict(seg_id=int(sid) if int_ids else sid, part_id=pid, part_label=part.get("part_label", ""),
                     process=seg["process"], category=seg["category"], interact=seg["interact"], phase=seg["phase"],
                     placement=part.get("placement"), is_hypo=int(part.get("is_hypo", 1)), confidence=part.get("confidence"),
                     geom_basis=part.get("geom_basis", ""), data_src=part.get("data_src", ""), area_ha=round(area, 6),
                     rep_value=str(part.get("rep_value", "")), rep_page=str(part.get("rep_page", "")))
        features.append({**{k: (_shp_text(v) if isinstance(v, str) else v) for k, v in attrs.items()}, "geometry": g})
        if len(attrs["geom_basis"].encode()) > 254 or len(attrs["data_src"].encode()) > 254:
            warns.append(f"{pid}: geom_basis/data_src longer than 254 bytes; truncated in the shapefile, full text in the CSV")
        fits = {**({"part": built["fit"]} if built.get("fit") else {}), **fit_from}
        evidence.append({
            **attrs,
            "pdf_pages": ",".join(map(str, pages_pdf)),
            "evidence_type": part.get("evidence_type", ""),
            "quotations": "\n\n".join(
                f"„{q['text']}“ (PDF p. {q.get('pdf_page')}, printed p. {q.get('printed_page')}{'–' + str(q['printed_page_end']) if q.get('printed_page_end') else ''})"
                for q in built["quotes"]),
            "quotations_json": _jsonable(built["quotes"]),
            "figure_refs": ",".join(str(f) for f in part.get("figures", [])),
            "visual_observations_json": _jsonable([visuals[str(f)] for f in part.get("figures", []) if str(f) in visuals]
                                                  + part.get("visual_notes", [])),
            "table_observations_json": _jsonable([tables[str(t)] for t in part.get("tables", []) if str(t) in tables]),
            "image_date_relevance": part.get("image_date_relevance", ""),
            "visual_identification_method": part.get("visual_identification_method", ""),
            "georeferencing_json": _jsonable({gid: georefs[gid].info() for gid in part.get("georef", []) if gid in georefs}),
            "georeferencing_limitations": part.get("georeferencing_limitations", "") or "; ".join(
                f"{gid}: RMS {georefs[gid].rms:.2f} m, max {georefs[gid].max_res:.2f} m, "
                + (", ".join(f"check {c.get('label', '')} {c['error_m']:.2f} m" for c in georefs[gid].checks) or "no independent check")
                + (f"; {georefs[gid].cfg.get('limitations')}" if georefs[gid].cfg.get("limitations") else "")
                for gid in part.get("georef", []) if gid in georefs),
            "reasoning": part.get("reasoning", ""),
            "uncertainty": part.get("uncertainty", ""),
            "alternative_hypotheses": part.get("alternatives", ""),
            "reason_placement": part.get("reason_placement", ""),
            "reason_is_hypo": part.get("reason_is_hypo", ""),
            "reason_confidence": part.get("reason_confidence", ""),
            "rule_5d": int(bool(part.get("rule_5d"))),
            "parameter_json": _jsonable(part.get("parameter") or {}),
            "fit_json": _jsonable(fits),
            "fit_is_independent_check": "no (fitted)" if fits else "not applicable",
            "sensitivity_json": _jsonable(built.get("sensitivity") or []),
            "checks_json": _jsonable(checks),
        })

    if not only:
        covered = {str(e["seg_id"]) for e in evidence}
        for s in seg_ids:
            if s not in covered:
                errors.append(f"segment {s} has no part (rule 6: every segment must be placed)")

    global_checks = []
    for c in spec.get("checks", []):
        try:
            global_checks.append(_check(ev, c))
        except Exception as exc:
            errors.append(f"check {c.get('label', '')}: {exc}")
    chain_info = {}
    for name in spec.get("chainages", {}):
        try:
            chain_info[name] = ev.chainage(name).info()
        except Exception as exc:
            errors.append(f"chainage {name}: {exc}")

    result = dict(parts=len(features), errors=errors, warnings=warns)
    if not write or not features:
        return result

    gdf = gpd.GeoDataFrame(features, geometry="geometry", crs=run.crs)[SHP_FIELDS + ["geometry"]]
    run.out.mkdir(parents=True, exist_ok=True)
    for ext in ("shp", "shx", "dbf", "prj", "cpg"):
        (run.out / f"segments.{ext}").unlink(missing_ok=True)
    gdf.to_file(run.out / "segments.shp", driver="ESRI Shapefile", encoding="UTF-8", engine="pyogrio")
    (run.out / "segments.cpg").write_text("UTF-8", encoding="ascii")
    with (run.out / "segments_evidence.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EVIDENCE_FIELDS)
        w.writeheader()
        w.writerows(evidence)
    write_json(run.raw / "visual_evidence.json", list(visuals.values()))
    write_json(run.raw / "georeferencing.json", {k: v.info() for k, v in georefs.items()})
    write_json(run.out / "checks.json", dict(crs=run.crs, parts=len(features), chainages=chain_info, checks=global_checks,
                                             ref_fits=ev.ref_fits, georeferencing={k: {kk: v.info()[kk] for kk in ("rms_m", "max_residual_m", "ground_pixel_size_m", "independent_checks", "transformation")} for k, v in georefs.items()},
                                             errors=errors, warnings=warns))
    return result


def _check(ev: Evaluator, c: dict) -> dict:
    m = ev.measure(c["measure"])
    out = dict(label=c.get("label", ""), measure=c["measure"], value=_round(m["value"]), unit=m.get("unit", ""),
               report_value=c.get("report_value"), report_text=c.get("report_text", ""), page=c.get("page", ""),
               kind=c.get("kind", "comparison"), note=c.get("note", ""))
    for k in ("lateral_m", "mean", "cells", "z_from", "z_to", "along_m"):
        if k in m:
            out[k] = _round(m[k])
    rv = c.get("report_value")
    if isinstance(rv, (int, float)) and isinstance(m["value"], (int, float)) and out["kind"] not in ("ordering", "info", "derived"):
        out["difference"] = _round(m["value"] - rv)
        out["difference_pct"] = _round(100 * (m["value"] - rv) / rv) if rv else None
    return out


def _round(v):
    if isinstance(v, float):
        return round(v, 4) if math.isfinite(v) else None
    if isinstance(v, list):
        return [_round(x) for x in v]
    return v
