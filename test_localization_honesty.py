"""Integration checks for evidence-limited results, recovery and bounded acquisition."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from multi_hazard_pipeline.agents.localization_agent import localize_run
from multi_hazard_pipeline.errors import PipelineError
from test_localization_stage import FakeModel, make_package, make_pipeline_run


class EditedModel(FakeModel):
    def __init__(self, edit):
        super().__init__()
        self.edit = edit

    def complete_json(self, **kwargs):
        validate = kwargs["validate"]
        def edited(answer):
            self.edit(answer, kwargs)
            validate(answer)
        return super().complete_json(**{**kwargs, "validate": edited})


class HonestLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.package = make_package(self.root / "package")
        self.run = make_pipeline_run(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def localize(self, model, **kwargs):
        return localize_run(self.run, self.package, client=model, vision=False,
                            browser_check=False, log=lambda _: None, **kwargs)

    def test_frame_rejects_coordinates_and_tokens_outside_provided_candidates(self):
        from multi_hazard_pipeline.agents.localization_agent import Localizer
        from jsonschema import ValidationError

        def edit(answer, request):
            if request["response_schema"]["name"] == "localization_frame":
                answer["chainage_anchors"].append(dict(value=4, point=token, page=2,
                    quote="Der Murgang erreichte die Testbrücke bei hm 4"))

        localizer = Localizer(self.run, self.package, EditedModel(edit), vision=False, log=lambda _: None)
        localizer.prepare()
        localizer.build_facts()
        for token in ("xy:1000,600", "building:1", "source", "hm:4", "confluence:S999"):
            with self.subTest(token=token), self.assertRaises(ValidationError):
                localizer.frame_call()

    def test_resumed_frame_rejects_invented_coordinates(self):
        from multi_hazard_pipeline.agents.localization_agent import Localizer

        localizer = Localizer(self.run, self.package, FakeModel(), vision=False, log=lambda _: None)
        localizer.prepare()
        localizer.build_facts()
        localizer.frame_call()
        localizer.frame["chainage_anchors"].append(dict(value=4, point="xy:1000,600", page=2,
            quote="Der Murgang erreichte die Testbrücke bei hm 4"))
        localizer.saved_state = {"frame": localizer.frame}
        with self.assertRaisesRegex(ValueError, "provided point/feature candidate"):
            localizer.frame_call()

    def test_frame_accepts_verified_bridge_anchor(self):
        def edit(answer, request):
            if request["response_schema"]["name"] == "localization_frame":
                bridge = answer["named_features"][0]["candidate"]
                answer["chainage_anchors"].append(dict(value=4, point=bridge, page=2,
                    quote="Der Murgang erreichte die Testbrücke bei hm 4"))

        self.assertEqual(self.localize(EditedModel(edit), review=False)["status"], "delivered")

    def test_unresolved_is_a_complete_result_without_a_polygon(self):
        def unresolved(answer, request):
            if "segment" in answer:
                answer.update(parts=[], localization_status="unresolved", reason="No geographic anchor in supplied evidence",
                              location_confidence="unknown", boundary_confidence="unknown")
        result = self.localize(EditedModel(unresolved), review=False)
        self.assertEqual(result["status"], "delivered_with_unresolved")
        out = self.run / "localization" / "outputs"
        self.assertFalse((out / "segments.shp").exists())
        self.assertEqual(len(json.loads((out / "segments.geojson").read_text())["features"]), 0)
        results = json.loads((out / "segment_results.json").read_text())
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r["localization_status"] == "unresolved" for r in results))

    def test_native_reach_and_bridge_do_not_gain_unsupported_widths(self):
        def native(answer, request):
            if "segment" not in answer or answer["segment"] == "1":
                return
            part = answer["parts"][0]
            part.update(method="channel_reach" if answer["segment"] == "2" else "feature_geometry",
                        geometry_role="location", boundary_confidence="unknown")
            part["parameter"].update(value=None, basis="none")
        result = self.localize(EditedModel(native), review=False)
        self.assertEqual(result["status"], "delivered")
        data = json.loads((self.run / "localization/outputs/segments.geojson").read_text())
        features = {f["properties"]["seg_id"]: f for f in data["features"]}
        self.assertEqual(features["2"]["geometry"]["type"], "LineString")
        self.assertEqual(features["3"]["geometry"]["type"], "LineString")
        self.assertIsNone(features["2"]["properties"]["area_ha"])

    def test_named_bridge_can_be_a_point_with_unknown_damage_boundary(self):
        def point(answer, request):
            if answer.get("segment") == "3":
                part = answer["parts"][0]
                part["args"]["center"] = part["args"]["candidate"]
                part.update(method="location_point", geometry_role="location")
                part["parameter"].update(value=None, basis="none")
        result = self.localize(EditedModel(point), review=False)
        self.assertEqual(result["status"], "delivered")
        data = json.loads((self.run / "localization/outputs/segments.geojson").read_text())
        bridge = next(f for f in data["features"] if f["properties"]["seg_id"] == "3")
        self.assertEqual(bridge["geometry"]["type"], "Point")
        self.assertEqual(bridge["properties"]["boundary_confidence"], "unknown")

    def test_partial_run_resumes_without_repeating_completed_placements(self):
        first = self.localize(FakeModel(), only=["1"], review=False)
        self.assertEqual(first["status"], "partial")
        model = FakeModel()
        # The fixture learns this during frame; a resumed model does not repeat the frame call.
        checkpoint = json.loads((self.run / "localization/llm/checkpoint.json").read_text())
        model.bridge = checkpoint["frame"]["named_features"][0]["candidate"]
        resumed = self.localize(model, resume=True, review=False)
        self.assertEqual(resumed["status"], "delivered")
        self.assertEqual(model.calls, ["segment_placement", "segment_placement"])
        self.assertTrue((self.run / "localization/outputs/segment_results.csv").exists())

    def test_changed_inputs_refuse_resume_and_preserve_checkpoint(self):
        self.localize(FakeModel(), only=["1"], review=False)
        checkpoint = self.run / "localization/llm/checkpoint.json"
        original = checkpoint.read_bytes()
        translated = self.run / "translated.json"
        translated.write_text(translated.read_text() + "\n")
        with self.assertRaisesRegex(PipelineError, "inputs or settings changed"):
            self.localize(FakeModel(), resume=True, review=False)
        self.assertEqual(checkpoint.read_bytes(), original)

    def test_existing_results_are_not_deleted_without_resume(self):
        self.localize(FakeModel(), review=False)
        ledger = self.run / "localization/outputs/segment_results.csv"
        original = ledger.read_bytes()
        with self.assertRaisesRegex(PipelineError, "already exists"):
            self.localize(FakeModel(), review=False)
        self.assertEqual(ledger.read_bytes(), original)

    def test_processing_failure_stays_incomplete_even_when_export_succeeds(self):
        class FailingModel(FakeModel):
            def complete_json(self, **kwargs):
                if kwargs["response_schema"]["name"] == "segment_placement":
                    raise PipelineError("incomplete generation")
                return super().complete_json(**kwargs)
        result = self.localize(FailingModel(), review=False)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(set(result["processing_failures"]), {"1", "2", "3"})
        self.assertTrue((self.run / "localization/outputs/segments_map.html").exists())

    def test_review_issues_remain_revision_required_after_attempted_correction(self):
        def review_issue(answer, request):
            if request["response_schema"]["name"] == "localization_review":
                answer.update(status="revision_required", issues=[dict(segment="1", part_id="1a", code="scope",
                    message="Scope not established", suggested_action="Keep unresolved")])
        result = self.localize(EditedModel(review_issue))
        self.assertEqual(result["status"], "revision_required")
        self.assertTrue(result["review"]["issues"])

    def test_vision_review_cannot_pass_a_revision_status_without_issues(self):
        from PIL import Image
        Image.new("RGB", (30,30), "white").save(self.root / "report.pdf", "PDF")
        def revision(answer, request):
            if request["response_schema"]["name"] == "localization_review":
                answer.update(status="revision_required", issues=[])
        result = localize_run(self.run, self.package, client=EditedModel(revision), browser_check=False, log=lambda _: None)
        self.assertEqual(result["status"], "revision_required")

    def test_interrupted_acquisition_discards_candidate_dependent_checkpoint(self):
        self.localize(FakeModel(), only=["1"], review=False)
        path = self.run / "localization/llm/checkpoint.json"
        checkpoint = json.loads(path.read_text())
        checkpoint["data_refresh_pending"] = True
        path.write_text(json.dumps(checkpoint))
        model = FakeModel()
        result = self.localize(model, resume=True, review=False)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(model.calls[0], "localization_frame")
        self.assertEqual(model.calls.count("segment_placement"), 3)

    def test_acquisition_during_review_rebuilds_every_invalidated_segment(self):
        package = json.loads(self.package.read_text())
        package["acquisition"] = {"providers": [dict(id="extra", role="roads", name="extra", type="file", url="unused.gpkg")]}
        self.package.write_text(json.dumps(package))
        state = {"reviewed": False, "acquired": False}
        def edit(answer, request):
            if request["response_schema"]["name"] == "localization_review" and not state["reviewed"]:
                state["reviewed"] = True
                answer.update(status="revision_required", issues=[dict(segment="1", part_id="1a", code="missing",
                    message="Need another source", suggested_action="Request source")])
            elif answer.get("segment") == "1" and (request["user_payload"].get("correction") or {}).get("review_issues") and not state["acquired"]:
                state["acquired"] = True
                answer["data_requests"] = [dict(provider="extra", role="roads", query="Testbach", reason="Need source")]
        with patch("segment_mapping.acquisition.acquire_source", return_value=dict(status="success", request={})):
            result = self.localize(EditedModel(edit))
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(len(result["build"]["segment_results"]), 3)
        self.assertTrue(all(r["localization_status"] == "located" for r in result["build"]["segment_results"]))

    def test_incomplete_generation_has_one_lower_reasoning_recovery(self):
        from dataclasses import replace
        from multi_hazard_pipeline.config import DEFAULT_CONFIG
        from multi_hazard_pipeline.llm import ChatClient
        from multi_hazard_pipeline.agents.localization_agent import Localizer
        client = ChatClient(api_key="synthetic-key", config=replace(DEFAULT_CONFIG.llm, retries=1))
        localizer = Localizer(self.run, self.package, client, vision=False, log=lambda _: None)
        localizer.prepare()
        schema = dict(name="small", strict=True, schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
        responses = [{"status": "incomplete", "output": []}, {"status": "completed", "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{"ok":true}'}]}]}]
        with patch.object(ChatClient, "_post", side_effect=responses) as post:
            answer = localizer._call("small", "Return JSON", {}, schema, lambda _: None)
        self.assertTrue(answer["ok"])
        self.assertEqual(post.call_count, 2)
        self.assertTrue((self.run / "localization/llm/small_recovery.json").exists())

    def test_acquisition_refresh_rebuilds_previous_candidate_references(self):
        package = json.loads(self.package.read_text())
        package["acquisition"] = {"providers": [dict(id="extra", role="roads", name="extra", type="file", url="unused.gpkg")]}
        self.package.write_text(json.dumps(package))
        asked = []
        def request_data(answer, request):
            if answer.get("segment") == "2" and not asked:
                asked.append(True)
                answer["data_requests"] = [dict(provider="extra", role="roads", query="Testbach", reason="Need road identity")]
        result_dict = dict(status="success", request={"role": "roads", "query": "Testbach", "provider": "extra"})
        model = EditedModel(request_data)
        with patch("segment_mapping.acquisition.acquire_source", return_value=result_dict) as acquire:
            result = self.localize(model, review=False)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(acquire.call_count, 1)
        self.assertEqual(model.calls.count("localization_frame"), 2)
        calls_for_one = [r for r in model.requests if r["name"] == "segment_placement" and r["payload"]["segment"]["segment"] == "1"]
        self.assertEqual(len(calls_for_one), 2)

    def test_report_area_is_compared_without_automatic_fitting(self):
        from segment_mapping.templates import Compiler
        facts = {"candidates": [dict(id="S1", kind="streams", layer="s", name="A", expr="s"),
                                dict(id="C1", kind="catchment", layer="c", name="A", expr="c")]}
        compiler = Compiler(facts, dict(main_stream="S1", catchment="C1", named_features=[]), {}, chainage=False)
        part = compiler.compile_part("1", "1a", dict(method="reach_corridor", args=dict(from_point="mouth", to_point="source"),
            parameter=dict(value=2, basis="assumption", report_area_ha=7.5), is_hypo=1, confidence="low"))
        self.assertNotIn("fit_area_ha", part["parameter"])
        self.assertEqual(part["report_area_ha"], 7.5)


class OutlineIntegrationTests(unittest.TestCase):
    def test_resume_fingerprint_includes_referenced_figure_metadata_and_image(self):
        from multi_hazard_pipeline.agents.localization_state import input_fingerprint
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = make_package(root / "package")
            run = make_pipeline_run(root)
            figure = root / "candidate.json"
            figure.write_text(json.dumps({"image": "image.png", "georef": {}}))
            image = root / "image.png"
            image.write_bytes(b"original image")
            manifest = root / "outlines.json"
            manifest.write_text(json.dumps({"outlines": [{"candidate": "candidate.json"}]}))
            original = input_fingerprint(run, package, figure_manifest=manifest)
            figure.write_text(json.dumps({"image": "image.png", "georef": {"changed": True}}))
            changed = input_fingerprint(run, package, figure_manifest=manifest)
            self.assertNotEqual(original, changed)
            image.write_bytes(b"changed image")
            self.assertNotEqual(changed, input_fingerprint(run, package, figure_manifest=manifest))

    def test_only_verified_event_outline_enters_compiler(self):
        from PIL import Image
        from segment_mapping.figure_workflow import prepare_figure
        from multi_hazard_pipeline.agents.localization_figures import load_outline_manifest
        from segment_mapping.templates import Compiler, TemplateError
        from test_figure_workflow import candidate, LIMITS, INTERPRETATION
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (100, 100), "white").save(root / "image.png")
            prepare_figure(root / "image.png", root / "figure", candidate())
            interpretation = {**INTERPRETATION, "event_date": "2015-06", "event_confidence": "high",
                              "target_event_verified": True, "outline_explicit": True}
            entry = dict(id="F1", candidate="figure/figure_candidate.json", segments=["2"], limits=LIMITS,
                         pixel_geometry={"type": "Polygon", "coordinates": [[[10,10],[40,10],[40,40],[10,40],[10,10]]]},
                         interpretation=interpretation)
            manifest = root / "outlines.json"
            manifest.write_text(json.dumps({"outlines": [entry]}))
            figures, gaps = load_outline_manifest(manifest, "EPSG:32632")
            self.assertFalse(gaps)
            compiler = Compiler({"candidates": []}, {"named_features": []}, {}, False, figure_candidates=figures)
            part = compiler.compile_part("2", "2a", dict(method="report_outline", args={"candidate": "F1"},
                is_hypo=0, confidence="medium", geometry_role="event_extent", boundary_confidence="low"))
            self.assertEqual(part["placement"], "documented")
            with self.assertRaises(TemplateError):
                compiler.compile_part("3", "3a", dict(method="report_outline", args={"candidate": "F1"}))
            entry["interpretation"]["target_event_verified"] = False
            manifest.write_text(json.dumps({"outlines": [entry]}))
            figures, gaps = load_outline_manifest(manifest, "EPSG:32632")
            self.assertFalse(figures)
            self.assertIn("verified target event", gaps[0]["reason"])


if __name__ == "__main__":
    unittest.main()
