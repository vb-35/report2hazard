"""Offline tests for localization: spatial packages, text/visual evidence, templates and delivery."""
import json
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase

import numpy as np

from segment_mapping.runctx import Run, parse_segment_table, write_json

CRS = "EPSG:31254"
SEGMENTS = (
    "Segment\tEvent\tProcess\tGeneralized Category\tInteraction Type\tSediment Transport Phase\n"
    "1\tTestbach\tHeavy rain\tTriggering event\tProcess-topography\tDysconnectivity\n"
)
PIPE_PAGES = [
    "IAN Test Seite 10 Das Einzugsgebiet des Testbaches umfasst 2,5 km². Der Testbach mündet in den Hauptfluss.",
    "IAN Test Seite 11 Der Murgang erreichte die Testbrücke bei hm 4 und zerstörte das Gelän- der.",
    "IAN Test Seite 12 Im Mittellauf zwischen hm 2 und hm 12 kam es zu Erosion. Abbildung 3: Luftbild",
]


def make_package(root: Path) -> Path:
    """Synthetic spatial package: catchment, main stream into a receiving river, bridge, buildings, DEM."""
    import geopandas as gpd
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import LineString, Polygon, box

    root.mkdir(parents=True, exist_ok=True)
    xs = np.arange(0, 2000, 10) + 5
    ys = np.arange(2000, 0, -10) - 5
    X, Y = np.meshgrid(xs, ys)
    z = (1000 + 0.2 * Y + 0.15 * np.abs(X - 1000) + 0.02 * X).astype("float32")
    with rasterio.open(root / "dem.tif", "w", driver="GTiff", width=200, height=200, count=1, dtype="float32", crs=CRS,
                       transform=from_origin(0, 2000, 10, 10)) as ds:
        ds.write(z, 1)
    gpd.GeoDataFrame({"NAME": ["Testbach"], "CODE": ["T-1"]},
                     geometry=[Polygon([(600, 100), (1400, 100), (1400, 1950), (600, 1950)])], crs=CRS).to_file(root / "catchment.gpkg")
    gpd.GeoDataFrame({"NAME": ["Testbach", "Hauptfluss"], "CODE": ["T-1", "H"]},
                     geometry=[LineString([(1000, 1900), (1000, 100)]), LineString([(300, 100), (1900, 100)])], crs=CRS).to_file(root / "streams.gpkg")
    gpd.GeoDataFrame({"NAME": ["Testbrücke"]}, geometry=[LineString([(990, 500), (1010, 500)])], crs=CRS).to_file(root / "bridges.gpkg")
    gpd.GeoDataFrame({"name": ["", ""]}, geometry=[box(1100, 150, 1120, 170), box(1300, 400, 1315, 415)], crs=CRS).to_file(root / "buildings.gpkg")
    pkg = {"name": "synthetic", "crs": CRS,
           "layers": [{"name": "catchments", "path": "catchment.gpkg", "role": "catchment", "name_field": "NAME", "id_field": "CODE"},
                      {"name": "streams", "path": "streams.gpkg", "role": "streams", "name_field": "NAME", "id_field": "CODE"},
                      {"name": "bridges", "path": "bridges.gpkg", "role": "bridges", "name_field": "NAME"},
                      {"name": "buildings", "path": "buildings.gpkg", "role": "buildings"}],
           "rasters": [{"name": "dem", "path": "dem.tif", "role": "dem"}],
           "sources": [{"name": "streams", "provider": "synthetic", "licence": "test"}]}
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    return root / "package.json"


def make_pipeline_run(root: Path) -> Path:
    run = root / "run"
    run.mkdir(parents=True)
    chunks = [{"chunk_id": f"doc-page-{i + 1:04d}", "file": "report.pdf", "page": i + 1, "text": t, "translated_text": t}
              for i, t in enumerate(PIPE_PAGES)]
    write_json(run / "translated.json", {"chunks": chunks})
    rows = [dict(segment=1, event="Testbach", process="Heavy rain over the catchment", generalized_category="Triggering Event",
                 interaction_type="Process-topography", sediment_transport_phase="not applicable"),
            dict(segment=2, event="Testbach", process="Erosion in the middle course between hm 2 and hm 12",
                 generalized_category="Material Mobilization", interaction_type="Process-topography", sediment_transport_phase="Erosion"),
            dict(segment=3, event="Testbach", process="Debris flow damaged the Testbrücke",
                 generalized_category="Impact on permanent or temporary infrastructure", interaction_type="Process-structure",
                 sediment_transport_phase="Transportation")]
    write_json(run / "final_rows.json", {"rows": rows, "status": "approved"})
    ev = {1: (1, "Das Einzugsgebiet des Testbaches umfasst 2,5 km²."), 2: (3, "Im Mittellauf zwischen hm 2 und hm 12 kam es zu Erosion."),
          3: (2, "Der Murgang erreichte die Testbrücke bei hm 4")}
    write_json(run / "segments.json", {"segments": [{"segment": k, "evidence": [{"chunk_id": f"doc-page-{p:04d}", "quote": q}]}
                                                    for k, (p, q) in ev.items()]})
    write_json(run / "manifest.json", {"status": "approved", "inputs": [{"path": str(root / "report.pdf")}], "doc_id": "test"})
    return run


class FakeModel:
    timing_path = None

    def __init__(self):
        self.calls = []
        self.requests = []

    def complete_json(self, *, system_prompt, user_payload, validate, response_schema=None, **kw):
        name = response_schema["name"]
        self.calls.append(name)
        self.requests.append(dict(name=name, prompt=system_prompt, payload=user_payload, images=kw.get("images", [])))
        if name == "localization_frame":
            cands = user_payload["candidates"]
            pick = lambda kind, nm: next(c["id"] for c in cands if c["kind"] == kind and c["name"] == nm)
            self.bridge = pick("bridges", "Testbrücke")
            ans = {"main_stream": pick("streams", "Testbach"), "catchment": pick("catchment", "Testbach"),
                   "receiving_river": pick("streams", "Hauptfluss"),
                   "named_features": [{"report_name": "Testbrücke", "candidate": self.bridge, "certainty": "named", "basis": "same name", "page": 2}],
                   "chainage_anchors": [{"value": 0, "point": "mouth", "page": None, "quote": ""}],
                   "report_numbers": {"catchment_area_km2": 2.5, "main_stream_length_km": None}, "notes": ""}
        elif name == "segment_placement":
            seg = int(user_payload["segment"]["segment"])
            ev = user_payload["segment"]["evidence"][0]
            arg_keys = response_schema["schema"]["properties"]["parts"]["items"]["properties"]["args"]["properties"]
            args = {k: None for k in arg_keys}
            base = dict(label=f"part {seg}", parameter={"value": None, "basis": "none", "justification": "", "report_area_ha": None},
                        is_hypo=0, confidence="medium", evidence_type="text", quotes=[{"page": ev["page"], "text": ev["quote"]}],
                        rep_value="", reasoning="r", uncertainty="u", alternatives="", reason_is_hypo="h", reason_confidence="c")
            if seg == 1:
                part = dict(base, method="catchment", confidence="high", args=dict(args, candidate="catch"))
            elif seg == 2:
                part = dict(base, method="reach_corridor", alternatives="narrower bed-only band",
                            args=dict(args, from_point="hm:2", to_point="hm:12", max_dist_m=60),
                            parameter={"value": 4, "basis": "assumption", "justification": "no width stated", "report_area_ha": None})
            else:
                part = dict(base, method="feature", confidence="high", args=dict(args, candidate=self.bridge),
                            parameter={"value": 7, "basis": "data_measurement", "justification": "deck width", "report_area_ha": None})
            ans = {"segment": seg, "parts": [part]}
            if "visual_observations" in response_schema["schema"]["properties"]["parts"]["items"]["properties"]:
                part["visual_observations"] = []
                if kw.get("images"):
                    source = user_payload["image_sources"][0]
                    part["evidence_type"] = "mixed"
                    part["reasoning"] = f"Test observation from {source['source_id']}"
                    part["visual_observations"] = [{"source_id": source["source_id"],
                        "observation": "A red rectangle is visible.", "date_relevance": "unknown date",
                        "identification_method": "synthetic page; no geographic inference"}]
        else:
            ans = {"status": "pass", "summary": "ok", "issues": []}
        validate(ans)
        return ans


class LocalizationStageTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.pkg = make_package(self.tmp / "package")
        self.pipe = make_pipeline_run(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_package_text_and_facts(self):
        from segment_mapping.facts import Facts
        from segment_mapping.package import import_package
        from segment_mapping.report import import_text, pages_from_pipeline, resolve_quote

        run = Run(self.tmp / "toolkit")
        run.ensure_dirs()
        segs, header = parse_segment_table(SEGMENTS)
        run.config = dict(event="t", report="", crs="EPSG:4326", segments=segs, segment_header=header, rasters={}, printed_page_offset=None)
        run.save_config()
        import_package(run, self.pkg)
        run = Run(run.dir)
        self.assertEqual((run.crs, run.config["default_dem"]), (CRS, "dem"))
        self.assertIsNotNone(run.bbox())
        info = import_text(run, pages_from_pipeline(self.pipe))
        self.assertEqual((info["pages"], info["offset"]), (3, 9))
        self.assertTrue(resolve_quote(Run(run.dir), {"text": "zerstörte das Geländer"})["verified"])  # soft hyphen 'Gelän- der'
        f = Facts(Run(run.dir)).build()
        by = {(c["kind"], c["name"]): c for c in f["candidates"]}
        main, river = by[("streams", "Testbach")], by[("streams", "Hauptfluss")]
        self.assertEqual(main["facts"]["flows_into"], river["id"])
        self.assertIn(1, main["mention_pages"])  # genitive 'Testbaches' matched
        self.assertIn(2, by[("bridges", "Testbrücke")]["mention_pages"])

    def test_localize_stage_end_to_end(self):
        import geopandas as gpd

        from multi_hazard_pipeline.agents.localization_agent import localize_run

        model = FakeModel()
        res = localize_run(self.pipe, self.pkg, client=model, browser_check=False, log=lambda m: None)
        self.assertEqual(res["status"], "delivered", res.get("build"))
        self.assertEqual(model.calls, ["localization_frame"] + ["segment_placement"] * 3 + ["localization_review"])
        loc = self.pipe / "localization"
        g = gpd.read_file(loc / "outputs/segments.shp").set_index("part_id")
        self.assertEqual(list(g.index), ["1a", "2a", "3a"])
        self.assertEqual((g.loc["2a", "placement"], g.loc["2a", "is_hypo"], g.loc["2a", "confidence"]), ("modelled", 1, "low"))  # rule 5d
        self.assertEqual((g.loc["3a", "placement"], g.loc["3a", "confidence"]), ("approximate", "high"))
        manifest = json.loads((self.pipe / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["localization"]["status"], "delivered")
        self.assertIn("Text-only run", (loc / "outputs/data_report.md").read_text(encoding="utf-8"))

    def test_prepare_preserves_noncontiguous_pages_and_basemaps(self):
        from multi_hazard_pipeline.agents.localization_agent import Localizer
        from segment_mapping.report import resolve_quote

        page_map = {"1": 133, "2": 137, "3": 44}
        basemaps = [{"name": "Provided map", "url": "https://example.org/{z}/{x}/{y}.png", "attr": "Test"}]
        manifest = json.loads((self.pipe / "manifest.json").read_text(encoding="utf-8"))
        manifest["localization_input"] = {"printed_page_map": page_map, "basemaps": basemaps}
        write_json(self.pipe / "manifest.json", manifest)
        translated = json.loads((self.pipe / "translated.json").read_text(encoding="utf-8"))
        translated["chunks"][2].update(file="report_context.pdf", page=1)
        write_json(self.pipe / "translated.json", translated)

        localizer = Localizer(self.pipe, self.pkg, FakeModel(), log=lambda m: None)
        localizer.prepare()
        run = Run(self.pipe / "localization")
        self.assertEqual(run.config["printed_page_map"], page_map)
        self.assertEqual(run.config["basemaps"], basemaps)
        self.assertEqual([p["printed_page"] for p in run.pages()], [133, 137, 44])
        self.assertEqual((run.pages()[2]["file"], run.pages()[2]["file_page"]), ("report_context.pdf", 1))
        quote = resolve_quote(run, {"printed_page": 137, "text": "Der Murgang erreichte"})
        self.assertTrue(quote["verified"])
        self.assertEqual((quote["pdf_page"], quote["printed_page"]), (2, 137))
        captions = json.loads((run.raw / "report_captions.json").read_text(encoding="utf-8"))
        self.assertEqual(captions[0]["printed_page"], 44)
        self.assertIn("belongs to text page 2", localizer._verify_quote(3, "Der Murgang erreichte"))

    def test_vision_localize_end_to_end(self):
        import csv
        from PIL import Image
        from multi_hazard_pipeline.agents.localization_agent import localize_run

        picture = Image.new("RGB", (160, 120), "red")
        picture.save(self.tmp / "report.pdf", save_all=True, append_images=[picture, picture, picture])
        picture.close()
        before = (self.pipe / "final_rows.json").read_bytes()
        model = FakeModel()
        res = localize_run(self.pipe, self.pkg, client=model, browser_check=False, log=lambda m: None)
        self.assertEqual(res["status"], "delivered", res.get("build"))
        self.assertEqual(model.calls, ["localization_frame"] + ["segment_placement"] * 3 + ["localization_review"] * 3)
        self.assertTrue(all(len(r["images"]) == 4 for r in model.requests))
        placements = [r for r in model.requests if r["name"] == "segment_placement"]
        reviews = [r for r in model.requests if r["name"] == "localization_review"]
        self.assertEqual([r["payload"]["image_sources"][0]["file_page"] for r in placements], [1, 3, 2])
        self.assertEqual([r["images"] for r in placements], [r["images"] for r in reviews])
        loc = self.pipe / "localization"
        inventory = json.loads((loc / "llm/image_inventory.json").read_text(encoding="utf-8"))
        self.assertIsNone(inventory["sources"][3]["page"])  # image-only page has no invented text-page mapping
        self.assertEqual(inventory["uninspected"], [])
        self.assertEqual(len(list((loc / "llm/images").glob("*.jpg"))), 4)
        with (loc / "outputs/segments_evidence.csv").open(encoding="utf-8-sig", newline="") as f:
            evidence = list(csv.DictReader(f))
        self.assertTrue(all(r["evidence_type"] == "mixed" for r in evidence))
        self.assertTrue(all("A red rectangle" in r["visual_observations_json"] for r in evidence))
        self.assertTrue(all(r["placement"] != "documented" for r in evidence))
        report = (loc / "outputs/data_report.md").read_text(encoding="utf-8")
        self.assertIn("vision-assisted", report)
        self.assertNotIn("Text-only run", report)
        self.assertIn("file page 3", report)
        self.assertIn("3 imported text-page records", report)
        self.assertNotIn("base64,", (loc / "llm/segment_2.json").read_text(encoding="utf-8"))
        self.assertEqual((self.pipe / "final_rows.json").read_bytes(), before)

    def test_explicit_text_only(self):
        from unittest.mock import patch
        from multi_hazard_pipeline.agents.localization_agent import localize_run

        model = FakeModel()
        with patch("multi_hazard_pipeline.agents.localization_vision.ReportImages", side_effect=AssertionError("must not render")):
            result = localize_run(self.pipe, self.pkg, client=model, vision=False, browser_check=False, log=lambda m: None)
        self.assertEqual(result["status"], "delivered")
        self.assertFalse(any(r["images"] for r in model.requests))

    def test_rejects_unattached_visual_reference(self):
        from multi_hazard_pipeline.agents.localization_agent import Localizer

        class InvalidVisualModel(FakeModel):
            def complete_json(self, **kwargs):
                validate = kwargs.pop("validate")
                answer = super().complete_json(validate=lambda a: None, **kwargs)
                if kwargs["response_schema"]["name"] == "segment_placement":
                    answer["parts"][0]["visual_observations"] = [{"source_id": "image-9999", "observation": "invented",
                        "date_relevance": "unknown", "identification_method": "none"}]
                validate(answer)
                return answer

        localizer = Localizer(self.pipe, self.pkg, InvalidVisualModel(), log=lambda m: None)
        localizer.prepare()
        localizer.build_facts()
        localizer.frame_call()
        with self.assertRaisesRegex(ValueError, "was not attached"):
            localizer.segment_call(localizer.run.segments[0])

    def test_unapproved_run_is_refused(self):
        from multi_hazard_pipeline.agents.localization_agent import localize_run
        from multi_hazard_pipeline.errors import PipelineError

        write_json(self.pipe / "manifest.json", {"status": "awaiting_human_review", "inputs": []})
        with self.assertRaises(PipelineError):
            localize_run(self.pipe, self.pkg, client=FakeModel(), browser_check=False, log=lambda m: None)

    def test_classification_rules_raise_for_repair(self):
        from segment_mapping.templates import Compiler, TemplateError

        facts = {"candidates": [{"id": "S1", "kind": "streams", "name": "A", "expr": {}, "layer": "s"},
                                {"id": "C1", "kind": "catchment", "name": "A", "expr": {}, "layer": "c"},
                                {"id": "P1", "kind": "names", "name": "X", "expr": {}, "geom_type": "Point", "layer": "n"},
                                {"id": "P2", "kind": "names", "name": "Y", "expr": {}, "geom_type": "Polygon", "layer": "n"}]}
        comp = Compiler(facts, {"main_stream": "S1", "catchment": "C1", "named_features": []}, {"b": {"role": "buildings"}}, chainage=False)
        with self.assertRaises(TemplateError):  # proximity is not damage
            comp.compile_part("1", "1a", {"method": "buildings_near", "args": {"center": "P1", "radius_m": 50}, "is_hypo": 0, "confidence": "low"})
        with self.assertRaises(TemplateError):  # high needs a 'named' frame feature
            comp.compile_part("1", "1a", {"method": "feature", "args": {"candidate": "P2"}, "is_hypo": 0, "confidence": "high"})
        # an assumed width on a point feature is rule 5d: high/0 is overridden to low/1, not an error
        cp = comp.compile_part("1", "1a", {"method": "feature", "args": {"candidate": "P1"}, "is_hypo": 0, "confidence": "high",
                                           "parameter": {"value": 10, "basis": "assumption"}})
        self.assertEqual((cp["placement"], cp["is_hypo"], cp["confidence"], cp["rule_5d"]), ("modelled", 1, "low", True))
        with self.assertRaises(TemplateError):  # no stationing in the frame
            comp.point("hm:4")


class RunPageTests(TestCase):
    def test_constant_offset_is_backward_compatible(self):
        run = Run(".")
        run.config = {"printed_page_offset": 9}
        self.assertEqual((run.printed(2), run.pdf_page(11)), (11, 2))

    def test_explicit_map_overrides_offset(self):
        run = Run(".")
        run.config = {"printed_page_offset": 9, "printed_page_map": {"1": 133, "2": 137, "3": 44}}
        self.assertEqual((run.printed(2), run.pdf_page(137)), (137, 2))

    def test_duplicate_printed_page_is_ambiguous(self):
        run = Run(".")
        run.config = {"printed_page_map": {"1": 10, "2": 10}}
        with self.assertRaisesRegex(ValueError, "multiple PDF pages"):
            run.pdf_page(10)


class VisionSourceTests(TestCase):
    def test_companion_pdf_mapping_limit_and_cache(self):
        from PIL import Image
        from unittest.mock import patch
        from multi_hazard_pipeline.agents.localization_vision import ReportImages
        from multi_hazard_pipeline.vision import load_images

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with Image.new("RGB", (80, 100), "red") as red, Image.new("RGB", (80, 100), "blue") as blue:
                red.save(root / "report.pdf", save_all=True, append_images=[red] * 9)
                blue.save(root / "companion.pdf")
            pages = [{"pdf_page": 1, "file": "report.pdf", "file_page": 7, "printed_page": 133},
                     {"pdf_page": 2, "file": "companion.pdf", "file_page": 1, "printed_page": 44}]
            reports = ReportImages({"inputs": [{"path": str(root / f)} for f in ("report.pdf", "companion.pdf")]},
                                   pages, root / "llm/images", lambda m: None)
            with patch("multi_hazard_pipeline.agents.localization_vision.load_images", wraps=load_images) as render:
                images, sources = reports.attach(reports.select([2, 1]))
                count = render.call_count
                repeated, _ = reports.attach([s["source_id"] for s in sources])
                self.assertEqual(render.call_count, count)
            self.assertEqual(len(images), 8)
            self.assertEqual(images, repeated)
            self.assertEqual([(s["filename"], s["file_page"], s["page"], s["printed_page"]) for s in sources[:2]],
                             [("companion.pdf", 1, 2, 44), ("report.pdf", 7, 1, 133)])
            self.assertEqual([s["file_page"] for s in sources[2:4]], [6, 8])
            self.assertEqual(len(reports.summary()["uninspected"]), 3)

    def test_render_failure_is_recorded_and_not_retried(self):
        from PIL import Image
        from unittest.mock import patch
        from multi_hazard_pipeline.agents.localization_vision import ReportImages

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with Image.new("RGB", (50, 50)) as pic:
                pic.save(root / "picture.png")
            reports = ReportImages({"inputs": [{"path": str(root / "picture.png")}]}, [], root / "llm/images", lambda m: None)
            with patch("multi_hazard_pipeline.agents.localization_vision.load_images", side_effect=ValueError("bad image")) as render:
                self.assertEqual(reports.attach(reports.select([])), ([], []))
                self.assertEqual(reports.attach(reports.select([])), ([], []))
                self.assertEqual(render.call_count, 1)
            self.assertIn("bad image", reports.summary()["errors"][0])
            self.assertEqual(len(reports.summary()["uninspected"]), 1)

    def test_ambiguous_filenames_are_not_attached(self):
        from multi_hazard_pipeline.agents.localization_vision import ReportImages

        reports = ReportImages({"inputs": [{"path": "a/report.pdf"}, {"path": "b/report.pdf"}]}, [],
                               Path("unused"), lambda m: None)
        self.assertEqual(reports.sources, {})
        self.assertEqual(len(reports.errors), 2)
        self.assertTrue(all("ambiguous" in e for e in reports.errors))

    def test_localization_client_configuration(self):
        from dataclasses import replace
        from unittest.mock import patch
        from multi_hazard_pipeline.config import DEFAULT_CONFIG
        from multi_hazard_pipeline.agents.localization_agent import Localizer, localize_run

        config = replace(DEFAULT_CONFIG, llm=replace(DEFAULT_CONFIG.llm, api_base_url="https://text.invalid/v1"))
        with patch("multi_hazard_pipeline.llm.ChatClient.from_config") as client, patch.object(Localizer, "run_all", return_value={}):
            localize_run("unused", "package.json", config=config)
            llm = client.call_args.args[0]
            self.assertEqual((llm.api_base_url, llm.text_endpoint, llm.max_request_chars),
                             ("http://llm.igf.oeaw.ac.at/v1", "/responses", 12_000_000))
            localize_run("unused", "package.json", config=config, vision=False)
            self.assertEqual(client.call_args.args[0].api_base_url, "https://text.invalid/v1")
            self.assertEqual(client.call_args.args[0].text_endpoint, "/chat/completions")
            localize_run("unused", "package.json", config=config, api_base_url="https://custom.invalid/v1", model="vision-model")
            self.assertEqual(client.call_args.args[0].api_base_url, "https://custom.invalid/v1")
            self.assertEqual(client.call_args.args[0].model, "vision-model")


class NameMatchingTests(TestCase):
    def test_exact_match_wins_over_spelling_variant(self):
        from segment_mapping.facts import _token_match, find_mentions

        self.assertEqual(_token_match("seigesalm", "seigesalm"), "exact")  # not 'fuzzy' via the ai-variant
        self.assertEqual(_token_match("saigesbach", "seigesbach"), "exact")  # ei/ai spelling variant
        pages = [{"pdf_page": 1, "text": "Die Erosion beginnt unterhalb der Seigesalm bei hm 28."}]
        self.assertTrue(find_mentions(pages, {"Seigesalm": []}, strict={"Seigesalm"}))
        self.assertFalse(find_mentions([{"pdf_page": 1, "text": "Es kam anders als erwartet."}], {"Anders": []}, strict={"Anders"}))


class PointTokenTests(TestCase):
    def test_mouth_and_source_follow_the_reach_stream(self):
        from segment_mapping.templates import Compiler

        facts = {"candidates": [{"id": "S1", "kind": "streams", "name": "Main", "expr": {}, "layer": "s"},
                                {"id": "S4", "kind": "streams", "name": "Trib", "expr": {}, "layer": "s", "facts": {"flows_into": "S1"}},
                                {"id": "C1", "kind": "catchment", "name": "Main", "expr": {}, "layer": "c"}]}
        comp = Compiler(facts, {"main_stream": "S1", "catchment": "C1", "named_features": []}, {}, chainage=False)
        self.assertEqual(comp.point("source"), {"end": "main"})
        self.assertEqual(comp.point("source", line="S4"), {"end": "S4"})
        self.assertEqual(comp.point("confluence:S4", line="S4"), {"start": "S4"})
        self.assertEqual(comp.point("confluence:S4"), {"endpoint_near": {"line": "S4", "to": "S1"}})

    def test_hm_on_tributary_is_projected_and_distance_checked(self):
        from segment_mapping.templates import Compiler

        facts = {"candidates": [{"id": "S1", "kind": "streams", "name": "Main", "expr": {}, "layer": "s"},
                                {"id": "S4", "kind": "streams", "name": "Trib", "expr": {}, "layer": "s"},
                                {"id": "C1", "kind": "catchment", "name": "Main", "expr": {}, "layer": "c"}]}
        comp = Compiler(facts, {"main_stream": "S1", "catchment": "C1", "named_features": [],
                                "chainage_anchors": [{"value": 0, "point": "mouth"}]}, {}, chainage=True)
        part = comp.compile_part("2", "2a", {"method": "reach_corridor", "args": {"stream": "S4", "from_point": "source", "to_point": "hm:18.3"},
                                             "parameter": {"value": 3, "basis": "assumption"}, "is_hypo": 1, "confidence": "low"})
        line = part["geometry"]["terrain"]["channels"][0]["between"]
        self.assertEqual((line["line"], line["from"]), ("S4", {"end": "S4"}))
        self.assertEqual(comp.near_checks, [("S4", "hm:18.3")])

    def test_valley_floor_banks_look_downstream(self):
        from shapely.geometry import Point
        from segment_mapping.geom import Evaluator
        from segment_mapping.templates import Compiler

        # Canonical stream heads north (mouth to source), so downstream left is east.
        comp = Compiler({"candidates": []}, {"main_stream": "main", "receiving_river": "river"}, {}, False)
        evaluator = Evaluator(None, {"refs": {"river": {"line": [[0, 0], [0, 100]]}}})
        for side, inside, outside in [("left", (10, 50), (-10, 50)),
                                       ("right", (-10, 50), (10, 50)),
                                       ("east", (10, 50), (-10, 50))]:
            with self.subTest(side=side):
                geometry = comp._geometry("valley_floor", dict(river="river", upstream="xy:0,80",
                                          downstream="xy:0,20", max_dist_m=30, side=side))
                footprint = evaluator.geom(geometry["terrain"]["within"])
                self.assertTrue(footprint.contains(Point(inside)))
                self.assertFalse(footprint.contains(Point(outside)))
