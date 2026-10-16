"""Text-only localization of approved segments (constrained orchestration over the segment_mapping toolkit).

Python owns control flow, geometry and validation; the model makes small, schema-checked choices:
one frame call (main stream, catchment, named features, stationing anchors), one call per segment (methods from a
fixed menu with candidate ids and parameters), targeted repairs, and an optional self-review.
"""
from __future__ import annotations

import shutil
import string
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ..config import DEFAULT_CONFIG, PipelineConfig
from ..core import read_json, run_lock, write_json
from ..errors import PipelineError

FRAME_PROMPT = """
You identify the geographic frame of a natural-hazard event report so that its causal segments can be placed on a map.
You receive English and original-language text of the most relevant report pages, the segments, and CANDIDATE GIS
features (id, kind, name, facts computed by code). Never invent coordinates or measurements. Return one JSON object only.

Choose:
- main_stream: candidate S# of the torrent/stream whose event the report describes.
- catchment: candidate C# of that stream's catchment.
- receiving_river: candidate S# the main stream flows into (see the facts 'flows_into'), or null.
- named_features: every place, bridge, road, farm or stream name the report uses to locate event effects, with the
  matching candidate: certainty "named" when the report name matches the candidate name (spelling variants allowed);
  "inferred" when the report uses another or local name but exactly one candidate fits (say why in basis);
  "not_found" when no candidate fits (candidate null). page = PDF page of the mention.
- chainage_anchors: report stations (hm = hectometres = 100 m from the mouth) that the report ties to an IDENTIFIABLE
  POINT: the mouth, a tributary confluence ("confluence:S#" with the tributary's id) or a bridge/road/place candidate
  (its id). Example: "Kaelberalmbach (tributary at hm 18.3)" -> {"value": 18.3, "point": "confluence:S4"}.
  Stations that only bound reaches ("from hm 6 to hm 32", slope sections, profile marks) are NOT anchors; they are used
  later as hm points. A stream id alone is never a point. Give page and an exact quote in the ORIGINAL language.
  Add {"value": 0, "point": "mouth"} when the report counts from the mouth. Empty list if no station is tied to a point.
- report_numbers: catchment area (km2) and main stream length (km) as stated by the report, else null.
- notes: one or two sentences on anything ambiguous.
""".strip()

SEGMENT_PROMPT = """
You place ONE segment of an approved natural-hazard event chain on a map using text evidence and the candidate GIS
features. This is a text-only run: you cannot see figures or maps. Return {"segment": <id>, "parts": [...]} with 1-4
parts; each part becomes one polygon a reviewer checks. Choose a method from the menu and fill its args; set unused
args to null. Point tokens: mouth, source, hm:<value>, confluence:S#, a candidate id (e.g. B3, P2, K1),
building:<fid>, xy:<x>,<y>.

Rules:
1. Every segment needs at least one part, even if approximate. Use the most specific location the text supports.
2. Conditions or triggers concerning the whole catchment: catchment (elevation_band if the text states an elevation).
3. Processes along the channel: reach_corridor between the stated stations or named points. Valley floor, fan or
   receiving river: valley_floor between named limits (upstream, downstream), or reach_corridor on the lowest reach.
4. Damage to a named bridge, road or building: feature or road_stretch; a stated length goes in length_m and
   args.center is the named place of the damage (e.g. the bridge), not a kilometre marker unless the text names that km.
   A named farm, sawmill or house given as a place point: buildings_near with a small radius (30-60 m).
   Damaged buildings without a name: buildings_near (is_hypo 1; proximity is not proof of damage).
   A stretch along a stream or river (e.g. "filled over 450 m") is a reach_corridor or valley_floor, never a circle.
   point_area is the last resort for a located point without any extent (radius at most 500 m).
5. parameter.value with parameter.basis: "report_value" only if the report states that number for this part;
   "data_measurement" if measured from the provided data; otherwise "assumption". With "assumption" the extent has no
   basis in the report, so code makes the part modelled, low, is_hypo 1 with sensitivity: then you MUST also write
   alternatives (other plausible extents or readings). If the report states an AREA for exactly this process and place,
   set report_area_ha and code fits the polygon to it; choose limits wide enough to contain that area (max_dist_m).
6. is_hypo 1 when it is uncertain which place or feature is meant (several candidates, local name not found, inferred
   identity). confidence high only for a feature the report names that matches exactly one candidate; medium when the
   location follows from stationing, named limits or a single match; low for a choice among several candidates,
   inferred identity, or an extent without basis.
7. quotes: 1-3 exact quotations in the ORIGINAL language copied from the segment evidence or the 'original' page text,
   each with its page number. Never translate or shorten inside a quote.
8. label (short), reasoning (why this place and method), uncertainty, alternatives (required when is_hypo is 1),
   reason_is_hypo, reason_confidence, rep_value (a report number used for comparison, or "").
If a correction is supplied, fix exactly the reported problems and keep correct parts unchanged.
""".strip()

REVIEW_PROMPT = """
You review automatically placed map parts for the segments of a natural-hazard event report (text-only placement).
Check each part against the rules: the place matches the segment text; the method fits the process (catchment for
catchment-wide conditions, channel reaches for channel processes, valley floor for deposition/flooding, features for
named infrastructure); proximity is never treated as proof of damage; confidence high only for a uniquely named
feature; alternatives are given for hypotheses; quotes support the part. Report only real problems as issues, with
the segment id, the part_id, a short code, the problem and the concrete fix. status "pass" if there are no issues.
""".strip()

POINT_TOKEN_HELP = ("mouth | source (of the stream the reach runs along; main stream by default) | hm:<value> (main stream only) | "
                    "confluence:S# | <candidate id> | building:<fid> | xy:<x>,<y>")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _nullable(t):
    return {"type": [t, "null"]}


def frame_schema(cands: list[dict]) -> dict:
    ids = [c["id"] for c in cands]
    streams = [c["id"] for c in cands if c["kind"] == "streams"]
    catch = [c["id"] for c in cands if c["kind"] == "catchment"]
    feature = {"type": "object", "additionalProperties": False,
               "properties": {"report_name": {"type": "string"}, "candidate": {"type": ["string", "null"], "enum": ids + [None]},
                              "certainty": {"type": "string", "enum": ["named", "inferred", "not_found"]},
                              "basis": {"type": "string"}, "page": _nullable("integer")},
               "required": ["report_name", "candidate", "certainty", "basis", "page"]}
    anchor = {"type": "object", "additionalProperties": False,
              "properties": {"value": {"type": "number"}, "point": {"type": "string"}, "page": _nullable("integer"),
                             "quote": {"type": "string"}},
              "required": ["value", "point", "page", "quote"]}
    numbers = {"type": "object", "additionalProperties": False,
               "properties": {"catchment_area_km2": _nullable("number"), "main_stream_length_km": _nullable("number")},
               "required": ["catchment_area_km2", "main_stream_length_km"]}
    return {"name": "localization_frame", "strict": True, "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {"main_stream": {"type": "string", "enum": streams}, "catchment": {"type": "string", "enum": catch},
                       "receiving_river": {"type": ["string", "null"], "enum": streams + [None]},
                       "named_features": {"type": "array", "items": feature},
                       "chainage_anchors": {"type": "array", "items": anchor},
                       "report_numbers": numbers, "notes": {"type": "string"}},
        "required": ["main_stream", "catchment", "receiving_river", "named_features", "chainage_anchors", "report_numbers", "notes"]}}


def segment_schema(seg_id: int | str, cand_ids: list[str], methods: list[str]) -> dict:
    from segment_mapping.templates import ARG_KEYS

    num = ("min_elev_m", "max_elev_m", "max_dist_m", "length_m", "radius_m")
    boo = ("use_road_land", "within_catchment")
    idlike = ("candidate", "stream", "river")
    args = {}
    for k in ARG_KEYS:
        if k in num:
            args[k] = _nullable("number")
        elif k in boo:
            args[k] = _nullable("boolean")
        elif k in idlike:
            args[k] = {"type": ["string", "null"], "enum": cand_ids + ["main", "catch", "river", None]}
        elif k == "side":
            args[k] = {"type": ["string", "null"], "enum": ["north", "south", "east", "west", "left", "right", None]}
        else:
            args[k] = _nullable("string")
    part = {"type": "object", "additionalProperties": False, "properties": {
        "label": {"type": "string"}, "method": {"type": "string", "enum": methods},
        "args": {"type": "object", "additionalProperties": False, "properties": args, "required": list(ARG_KEYS)},
        "parameter": {"type": "object", "additionalProperties": False, "properties": {
            "value": _nullable("number"), "basis": {"type": "string", "enum": ["report_value", "data_measurement", "assumption", "none"]},
            "justification": {"type": "string"}, "report_area_ha": _nullable("number")},
            "required": ["value", "basis", "justification", "report_area_ha"]},
        "is_hypo": {"type": "integer", "enum": [0, 1]}, "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence_type": {"type": "string", "enum": ["text", "table", "mixed"]},
        "quotes": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "properties": {"page": {"type": "integer"}, "text": {"type": "string"}},
                                               "required": ["page", "text"]}},
        "rep_value": {"type": "string"}, "reasoning": {"type": "string"}, "uncertainty": {"type": "string"},
        "alternatives": {"type": "string"}, "reason_is_hypo": {"type": "string"}, "reason_confidence": {"type": "string"}},
        "required": ["label", "method", "args", "parameter", "is_hypo", "confidence", "evidence_type", "quotes", "rep_value",
                     "reasoning", "uncertainty", "alternatives", "reason_is_hypo", "reason_confidence"]}
    return {"name": "segment_placement", "strict": True, "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {"segment": {"enum": [seg_id]}, "parts": {"type": "array", "items": part, "minItems": 1, "maxItems": 4}},
        "required": ["segment", "parts"]}}


def review_schema(seg_ids: list) -> dict:
    issue = {"type": "object", "additionalProperties": False,
             "properties": {"segment": {"enum": seg_ids}, "part_id": {"type": "string"}, "code": {"type": "string"},
                            "message": {"type": "string"}, "suggested_action": {"type": "string"}},
             "required": ["segment", "part_id", "code", "message", "suggested_action"]}
    return {"name": "localization_review", "strict": True, "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {"status": {"type": "string", "enum": ["pass", "revision_required"]}, "summary": {"type": "string"},
                       "issues": {"type": "array", "items": issue}},
        "required": ["status", "summary", "issues"]}}


class Localizer:
    def __init__(self, artifact_dir: str | Path, package: str | Path, client, config: PipelineConfig = DEFAULT_CONFIG,
                 segments_table: str | Path | None = None, log: Callable[[str], None] = print):
        self.dir = Path(artifact_dir)
        self.loc = self.dir / "localization"
        self.package = Path(package).resolve()
        self.client = client
        self.config = config
        self.segments_table = Path(segments_table) if segments_table else None
        self.log = log
        self.calls = 0
        self.answers: dict[str, dict] = {}
        self.parts: dict[str, list[dict]] = {}

    # ================================================================== preparation
    def prepare(self):
        from segment_mapping.inspect_tools import inventory
        from segment_mapping.package import import_package, load_package
        from segment_mapping.report import clues, import_text, pages_from_pipeline
        from segment_mapping.runctx import Run, parse_segment_table, today

        manifest = read_json(self.dir / "manifest.json")
        if manifest.get("status") != "approved":
            raise PipelineError(f"localization needs an approved run (status is {manifest.get('status')!r})")
        pkg = load_package(self.package)
        if self.loc.exists():
            shutil.rmtree(self.loc)
        if self.segments_table:
            segments, header = parse_segment_table(self.segments_table.read_text(encoding="utf-8-sig"))
            self.evidence = {}
        else:
            rows = read_json(self.dir / "final_rows.json")["rows"]
            segments = [dict(seg_id=str(r["segment"]), event=r.get("event", ""), process=r["process"], category=r["generalized_category"],
                             interact=r["interaction_type"], phase=r["sediment_transport_phase"], extra={}) for r in rows]
            header = list(self.config.export_columns)
            seg_file = read_json(self.dir / "segments.json") if (self.dir / "segments.json").exists() else {"segments": []}
            self.evidence = {str(s["segment"]): s.get("evidence", []) for s in seg_file["segments"]}
        report = next((i["path"] for i in manifest.get("inputs", []) if i.get("path")), "")
        run = Run(self.loc)
        run.ensure_dirs()
        run.config = dict(event=segments[0].get("event") or manifest.get("doc_id", ""), report=report, crs=pkg["crs"],
                          printed_page_offset=None, segments=segments, segment_header=header, created=today(), rasters={},
                          mode="text", offline_map=True, pipeline_run=str(self.dir), model=self.config.llm.model)
        localization_input = manifest.get("localization_input") or {}
        for key in ("printed_page_map", "basemaps"):
            if key in localization_input:
                run.config[key] = deepcopy(localization_input[key])
        run.save_config()
        run.log_command(["localize (multi_hazard_pipeline)", str(self.dir), "--package", str(self.package)])
        import_package(run, self.package)
        run = Run(self.loc)
        import_text(run, pages_from_pipeline(self.dir))
        run = Run(self.loc)
        inventory(run)
        clues(run)
        self.run = run
        self.pages = run.pages()
        chunk_page = {cid: p["pdf_page"] for p in self.pages for cid in p.get("chunk_ids", [])}
        self.seg_quotes = {sid: [{"pdf_page": chunk_page.get(e.get("chunk_id")), "quote": e.get("quote", "")} for e in ev]
                           for sid, ev in self.evidence.items()}
        (self.loc / "llm").mkdir(parents=True, exist_ok=True)
        if hasattr(self.client, "timing_path"):
            self.client.timing_path = self.loc / "llm" / "timings.jsonl"

    def build_facts(self):
        from segment_mapping.facts import Facts

        self.facts = Facts(self.run).build()
        write_json(self.run.raw / "facts.json", self.facts)
        self.cands = {c["id"]: c for c in self.facts["candidates"]}
        if not any(c["kind"] == "streams" for c in self.facts["candidates"]) or not any(c["kind"] == "catchment" for c in self.facts["candidates"]):
            raise PipelineError("no stream or catchment candidates: check the package name fields and the report text")

    # ================================================================== model calls
    def _call(self, name: str, prompt: str, payload: dict, schema: dict, validate) -> Any:
        self.calls += 1
        self.log(f"  model call {self.calls}: {name}")
        out = self.client.complete_json(system_prompt=prompt, user_payload=payload, validate=validate, response_schema=schema)
        write_json(self.loc / "llm" / f"{name}.json", {"request": payload, "answer": out})
        return out

    def _page_view(self, numbers, chars=6000):
        by = {p["pdf_page"]: p for p in self.pages}
        return [dict(page=n, printed_page=by[n]["printed_page"], english=(by[n].get("translated_text") or by[n]["text"])[:chars],
                     original=by[n]["text"][:chars]) for n in numbers if n in by]

    def _verify_quote(self, page, text) -> str | None:
        from segment_mapping.report import resolve_quote

        q = resolve_quote(self.run, {"text": text, "pdf_page": page} if page else {"text": text})
        if not q["verified"]:
            return f"quote not found in the report text (page {page}): {text[:70]!r}; copy it exactly from the 'original' text"
        return None

    def frame_call(self):
        from segment_mapping.geom import Evaluator
        from segment_mapping.templates import Compiler

        counts = {}
        for c in self.facts["candidates"]:
            for p in c.get("mention_pages", []):
                counts[p] = counts.get(p, 0) + 1
        top = sorted(counts, key=lambda p: -counts[p])[:6]
        pages = sorted(set([1, 2] + top))[:8]
        payload = {
            "event": self.run.config["event"],
            "segments": [{"segment": s["seg_id"], "process": s["process"]} for s in self.run.segments],
            "candidates": [{"id": c["id"], "kind": c["kind"], "name": c["name"], "facts": c.get("description", "")[:400]}
                           for c in self.facts["candidates"]],
            "unresolved_quoted_names": self.facts["unresolved_quoted_names"],
            "numeric_clues": [{"page": c["page"], "snippet": c["snippet"]} for c in self.facts["numeric_clues"][:60]],
            "pages": self._page_view(pages, 5000),
            "point_tokens": "mouth | confluence:S# | <candidate id>",
        }

        def validate(ans):
            errs = []
            for f in ans.get("named_features", []):
                if f["certainty"] != "not_found" and not f.get("candidate"):
                    errs.append(f"named feature {f['report_name']!r}: candidate required unless certainty is not_found")
            values = [a["value"] for a in ans.get("chainage_anchors", [])]
            if values != sorted(values):
                errs.append("chainage anchors must be ordered by value")
            for a in ans.get("chainage_anchors", []):
                pt = a["point"].strip()
                if pt in self.cands and self.cands[pt]["kind"] == "streams":
                    errs.append(f"anchor hm {a['value']}: {pt} is a stream, not a point; use confluence:{pt} for a tributary "
                                "confluence, or drop the anchor if the report only gives a reach boundary")
                    continue
                if pt != "mouth" and a.get("quote"):
                    e = self._verify_quote(a.get("page"), a["quote"])
                    if e:
                        errs.append("anchor " + e)
            if errs:
                raise ValueError("; ".join(errs))
            comp = Compiler(self.facts, ans, self.run.config["package"]["layers"], chainage=bool(ans.get("chainage_anchors")))
            spec = {"refs": comp.refs(), "chainages": comp.chainages()}
            ev = Evaluator(self.run, spec)
            try:
                if spec["chainages"]:
                    info = ev.chainage("hm").info()
                    for a in info["anchors"]:
                        limit = max(150.0, 0.2 * a["value"] * 100)
                        if abs(a["offset_m"]) > limit:
                            errs.append(f"anchor hm {a['value']} ({a['label']}) lies {a['raw_m']} m from the mouth on the GIS line, "
                                        f"{a['offset_m']:+.0f} m from the stated station; check the point or the main stream")
                ev.geom("main"), ev.geom("catch")
            except Exception as exc:
                errs.append(f"frame geometry failed: {exc}")
            if errs:
                raise ValueError("; ".join(errs))

        self.frame = self._call("frame", FRAME_PROMPT, payload, frame_schema(self.facts["candidates"]), validate)
        self.compiler = Compiler(self.facts, self.frame, self.run.config["package"]["layers"], chainage=bool(self.frame.get("chainage_anchors")))
        self.base_spec = {"event": self.run.config["event"], "refs": self.compiler.refs(), "chainages": self.compiler.chainages()}
        from segment_mapping.geom import Evaluator as _E

        self.evaluator = _E(self.run, self.base_spec)
        self.chain_info = self.evaluator.chainage("hm").info() if self.base_spec["chainages"] else None

    def _methods(self) -> dict:
        from segment_mapping.templates import METHODS

        roles = {v.get("role") for v in self.run.config["package"]["layers"].values()}
        out = dict(METHODS)
        if "buildings" not in roles:
            out.pop("buildings_near")
        if not any(c["kind"] == "roads" for c in self.facts["candidates"]):
            out.pop("road_stretch")
        return out

    def segment_call(self, seg: dict, correction: dict | None = None):
        from segment_mapping.build import evaluate_parameter
        from segment_mapping.facts import evidence_bundle
        from segment_mapping.geom import polygons_only
        from segment_mapping.templates import TemplateError

        sid = seg["seg_id"]
        quotes = self.seg_quotes.get(sid, [])
        bundle = evidence_bundle(self.run, self.facts, quotes, seg["process"])
        methods = self._methods()
        frame_view = {
            "main_stream": {"id": self.frame["main_stream"], "name": self.cands[self.frame["main_stream"]]["name"]},
            "catchment": {"id": self.frame["catchment"], "name": self.cands[self.frame["catchment"]]["name"]},
            "receiving_river": self.frame.get("receiving_river"),
            "named_features": self.frame["named_features"],
            "chainage": ({"unit": "hm (100 m from the mouth)", "anchors": self.chain_info["anchors"],
                          "main_stream_length_hm": self.chain_info["line_length_units"]} if self.chain_info else "no stationing in the report"),
        }
        payload = {
            "segment": {"segment": sid, "event": seg.get("event", ""), "process": seg["process"], "generalized_category": seg["category"],
                        "interaction_type": seg["interact"], "sediment_transport_phase": seg["phase"],
                        "evidence": [{"page": q["pdf_page"], "quote": q["quote"]} for q in quotes]},
            "evidence_pages": self._page_view([p["page"] for p in bundle["pages"]], 5000),
            "candidates_on_evidence_pages": bundle["candidates_on_pages"],
            "numeric_clues": [{"page": c["page"], "snippet": c["snippet"]} for c in bundle["numeric_clues"]],
            "frame": frame_view,
            "candidates": [{"id": c["id"], "kind": c["kind"], "name": c["name"], "facts": c.get("description", "")[:220]}
                           for c in self.facts["candidates"]],
            "methods": methods,
            "point_tokens": POINT_TOKEN_HELP,
            "correction": correction,
        }
        compiled: list[dict] = []

        def validate(ans):
            errs = []
            compiled.clear()
            letters = iter(string.ascii_lowercase)
            for k, part in enumerate(ans["parts"]):
                pid = f"{sid}{next(letters)}"
                try:
                    cp = self.compiler.compile_part(sid, pid, part)
                except TemplateError as exc:
                    errs.append(f"part {k + 1} ({part.get('method')}): {exc}")
                    continue
                checks, self.compiler.near_checks = list(self.compiler.near_checks), []
                far = []
                for stream, tok in checks:
                    d = self.evaluator.geom(self.compiler.point(tok)).distance(self.evaluator.geom(stream))
                    if d > 200:
                        far.append(f"{tok} lies {d:.0f} m from {stream} (hm stations are on the main stream)")
                if far:
                    errs.append(f"part {k + 1}: " + "; ".join(far) + "; use mouth, source, confluence or candidate points on the tributary")
                    continue
                try:
                    g, _ = evaluate_parameter(self.evaluator, cp["geometry"], cp["parameter"], label=pid)
                    g = polygons_only(g)
                    if g.is_empty:
                        errs.append(f"part {k + 1}: the geometry is empty (check points/limits or choose another method)")
                except Exception as exc:
                    msg = str(exc)[:300]
                    if "outside the range reachable" in msg:
                        msg += (" -> the report area does not fit inside the chosen limits: increase args.max_dist_m (corridor "
                                "half-width) or widen the upstream/downstream limits; if it still cannot fit, set report_area_ha to "
                                "null, give a height value with basis 'assumption', and compare with the report area in uncertainty")
                    errs.append(f"part {k + 1}: geometry failed: {msg}")
                for q in cp["quotes"]:
                    e = self._verify_quote(q.get("pdf_page"), q["text"])
                    if e:
                        errs.append(f"part {k + 1}: {e}")
                if not cp["quotes"]:
                    errs.append(f"part {k + 1}: give at least one exact quote")
                empty = [f for f in ("label", "reasoning", "uncertainty", "reason_is_hypo", "reason_confidence") if not str(part.get(f, "")).strip()]
                if empty:
                    errs.append(f"part {k + 1}: these fields must not be empty: {', '.join(empty)}")
                if cp["is_hypo"] == 1 and not part.get("alternatives", "").strip():
                    why = (" (its extent rests on an assumed parameter, so rule 5d makes it a hypothesis)" if cp["rule_5d"] else "")
                    errs.append(f"part {k + 1}: alternatives are required for a hypothesis{why}: name other plausible places, "
                                "extents or readings")
                compiled.append(cp)
            if errs:
                raise ValueError("; ".join(errs))

        name = f"segment_{sid}" + ("_correction" if correction else "")
        ans = self._call(name, SEGMENT_PROMPT, payload, segment_schema(int(sid) if str(sid).isdigit() else sid, list(self.cands), list(methods)), validate)
        self.answers[sid] = ans
        self.parts[sid] = [deepcopy(c) for c in compiled]

    # ================================================================== assembly, build, review
    def assemble(self) -> dict:
        from segment_mapping.inspect_tools import ROLES

        spec = deepcopy(self.base_spec)
        spec["parts"] = [p for s in self.run.segments for p in self.parts.get(s["seg_id"], [])]
        nums = self.frame.get("report_numbers") or {}
        checks = []
        if nums.get("catchment_area_km2"):
            checks.append({"label": "Catchment area", "measure": {"area_km2": "catch"}, "report_value": nums["catchment_area_km2"], "kind": "comparison"})
        if nums.get("main_stream_length_km"):
            checks.append({"label": "Main stream length (GIS line, km)", "measure": {"length_m": "main"},
                           "report_value": nums["main_stream_length_km"] * 1000, "kind": "comparison"})
        checks.append({"label": "Catchment elevation range", "measure": {"dem_stats": "catch"}, "kind": "info"})
        spec["checks"] = checks
        roles = {v.get("role") for v in self.run.config["package"]["layers"].values()} | {"dem"}
        gaps = [{"searched": f"{ROLES[r]} in the spatial package", "result": "not provided",
                 "effect": "methods relying on it were not offered to the model"} for r in ("roads", "bridges", "buildings", "names", "road_km", "road_land") if r not in roles]
        gaps += [{"searched": f"report name {f['report_name']!r}", "result": "no matching feature in the package",
                  "effect": "parts referring to it are hypotheses or use broader areas"} for f in self.frame["named_features"] if f["certainty"] == "not_found"]
        gaps.append({"searched": "visual evidence (maps, orthophotos, photographs in the report)", "result": "not used (text-only run)",
                     "effect": "no report outline was digitized; no part is documented; photo-identified buildings are not placed"})
        spec["data_gaps"] = gaps
        anchors = self.chain_info["anchors"] if self.chain_info else []
        spec["map"] = {"context": [{"geom": "catch", "label": f"Catchment {self.cands[self.frame['catchment']]['name']} (context)", "color": "#555"},
                                   {"geom": "main", "label": self.cands[self.frame["main_stream"]]["name"], "color": "#2c7fb8"}]
                       + ([{"geom": "river", "label": self.cands[self.frame["receiving_river"]]["name"], "color": "#2c7fb8"}] if self.frame.get("receiving_river") else []),
                       **({"chainage_ticks": {"chainage": "hm", "values": [a["value"] for a in anchors]}} if anchors else {})}
        spec["report_notes"] = {
            "summary": (f"Automated text-only localization with model `{self.config.llm.model}` (segment_mapping constrained orchestration): "
                        f"1 frame call and one call per segment, {self.calls} model calls in total including corrections. "
                        f"Frame: main stream {self.cands[self.frame['main_stream']]['name']} ({self.frame['main_stream']}), catchment "
                        f"{self.cands[self.frame['catchment']]['name']} ({self.frame['catchment']}). {self.frame.get('notes', '')}"),
            "other": "Toolkit limitations: text-only mode; candidates come from name matching against the provided package, so local names absent "
                     "from it remain unresolved; terrain bands are screening models, not hydraulic simulations.",
        }
        write_json(self.run.dir / "scripts" / "placements.json", spec)
        return spec

    def build(self) -> dict:
        from segment_mapping.build import build
        from segment_mapping.runctx import Run

        self.run = Run(self.loc)
        r = build(self.run)
        write_json(self.loc / "llm" / "build_result.json", r)
        return r

    def review_call(self) -> dict:
        import csv

        csv.field_size_limit(50_000_000)
        with (self.run.out / "segments_evidence.csv").open(encoding="utf-8-sig", newline="") as f:
            ev = list(csv.DictReader(f))
        table = [{k: e[k][:500] for k in ("seg_id", "part_id", "part_label", "process", "placement", "is_hypo", "confidence", "rule_5d",
                                           "area_ha", "geom_basis", "quotations", "reasoning", "alternative_hypotheses")} for e in ev]
        seg_ids = [int(s["seg_id"]) if s["seg_id"].isdigit() else s["seg_id"] for s in self.run.segments]
        return self._call("review", REVIEW_PROMPT, {"parts": table, "frame": self.frame}, review_schema(seg_ids), lambda a: None)

    # ================================================================== orchestration
    def run_all(self, only: list[str] | None = None, review: bool = True, browser_check: bool = True) -> dict:
        from segment_mapping.deliver import deliver
        from segment_mapping.runctx import Run

        started = _now()
        self._status({"status": "running", "started_at": started, "package": str(self.package)})
        try:
            self.log("Preparing localization run folder")
            self.prepare()
            self.build_facts()
            self.log(f"{len(self.facts['candidates'])} candidates; frame call")
            self.frame_call()
            todo = [s for s in self.run.segments if not only or s["seg_id"] in only]
            failed = {}
            for s in todo:
                try:
                    self.segment_call(s)
                except Exception as exc:  # keep going; the build reports the missing segment
                    failed[s["seg_id"]] = str(exc)[:500]
                    self.log(f"  segment {s['seg_id']} failed: {exc}")
            self.assemble()
            result = self.build()
            rounds = 0
            while (result["errors"] or failed) and rounds < self.config.max_correction_rounds:
                rounds += 1
                by_seg: dict[str, list[str]] = {}
                for e in result["errors"]:
                    for s in self.run.segments:
                        sid = s["seg_id"]
                        if e.startswith(f"{sid}") and e[len(sid):len(sid) + 1].isalpha() or f"segment {sid} " in e:
                            by_seg.setdefault(sid, []).append(e)
                for sid, msg in failed.items():
                    by_seg.setdefault(sid, []).append(f"previous attempt failed: {msg}")
                if not by_seg:
                    break
                failed = {}
                for sid, errs in by_seg.items():
                    seg = next(s for s in self.run.segments if s["seg_id"] == sid)
                    try:
                        self.segment_call(seg, {"previous_answer": self.answers.get(sid), "errors": errs})
                    except Exception as exc:
                        failed[sid] = str(exc)[:500]
                self.assemble()
                result = self.build()
            review_error = None
            if review and not result["errors"]:
                try:
                    review = self.review_call()
                except Exception as exc:  # the review is optional: keep the built result
                    review, review_error = None, f"{type(exc).__name__}: {str(exc)[:300]}"
                    self.log(f"  self-review skipped: {review_error}")
            else:
                review = None
            if review and review["issues"]:
                by_seg = {}
                for i in review["issues"]:
                    by_seg.setdefault(str(i["segment"]), []).append(i)
                for sid, issues in by_seg.items():
                    seg = next((s for s in self.run.segments if s["seg_id"] == sid), None)
                    if seg is None:
                        continue
                    try:
                        self.segment_call(seg, {"previous_answer": self.answers.get(sid), "review_issues": issues})
                    except Exception as exc:
                        self.log(f"  review correction for segment {sid} failed: {exc}")
                self.assemble()
                result = self.build()
            out = {"status": "build_errors", "build": result}
            if not result["errors"]:
                d = deliver(Run(self.loc), browser_check=browser_check)
                out = {"status": "delivered" if not d["validation"]["errors"] else "validation_errors", "build": result,
                       "validation": d["validation"], "map_check": d["map_check"], "report": d["report"], "zip": d["zip"]}
            out.update(model_calls=self.calls, correction_rounds=rounds, review=review, review_error=review_error, frame=self.frame,
                       finished_at=_now(), started_at=started, package=str(self.package), outputs=str(self.loc / "outputs"))
            self._status(out)
            return out
        except Exception as exc:
            out = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "started_at": started, "finished_at": _now(), "model_calls": self.calls}
            self._status(out)
            raise

    def _status(self, info: dict):
        with run_lock(self.dir):
            manifest = read_json(self.dir / "manifest.json")
            keep = {k: v for k, v in info.items() if k not in ("frame",)}
            manifest["localization"] = {**manifest.get("localization", {}), **keep, "updated_at": _now()}
            write_json(self.dir / "manifest.json", manifest)


LOCALIZATION_MAX_OUTPUT_TOKENS = 24000  # about 3x the largest localization answer observed, reasoning included


def localize_run(artifact_dir, package, *, client=None, config: PipelineConfig = DEFAULT_CONFIG, segments_table=None,
                 only=None, review: bool = True, browser_check: bool = True, log=print,
                 max_output_tokens: int | None = LOCALIZATION_MAX_OUTPUT_TOKENS) -> dict:
    if client is None:
        from dataclasses import replace

        from ..llm import ChatClient

        llm = replace(config.llm, max_output_tokens=max_output_tokens or config.llm.max_output_tokens)
        config = replace(config, llm=llm)
        client = ChatClient.from_config(config.llm)
    return Localizer(artifact_dir, package, client, config, segments_table, log).run_all(only, review, browser_check)
