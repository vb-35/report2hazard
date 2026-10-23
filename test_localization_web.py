"""Offline interface regressions: queuing, approval isolation, and spatial outputs."""
import io
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

from multi_hazard_pipeline import pipeline, web
from multi_hazard_pipeline.core import read_json, resolve_path, write_json


class QueuedExecutor:
    def __init__(self):
        self.tasks = []

    def submit(self, task):
        self.tasks.append(task)


class LocalizationWebTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = resolve_path(self.temp.name)
        self.addCleanup(shutil.rmtree, self.root)
        inputs = self.root / "inputs"
        inputs.mkdir()
        (inputs / "report.txt").write_text("Heavy rain mobilized sediment.", encoding="utf-8")
        self.manifest = pipeline.create_run(inputs, self.root / "results")
        self.run = Path(self.manifest["artifact_dir"])
        self.manifest["status"] = "approved"
        pipeline.save_manifest(self.run, self.manifest)
        write_json(self.run / "final_rows.json", {"status": "approved", "rows": [{"segment": 1}]})
        self.original_rows = (self.run / "final_rows.json").read_bytes()
        self.executor = QueuedExecutor()
        self.app = web.create_app(self.root / "results", executor=self.executor)
        self.app.testing = True
        self.client = self.app.test_client()
        self.url = f"/runs/{self.manifest['run_id']}/localization"
        package_dir = self.root / "package"
        package_dir.mkdir()
        for name in ("catchment.gpkg", "streams.gpkg", "dem.tif"):
            (package_dir / name).write_bytes(b"offline test placeholder")
        self.package = package_dir / "package.json"
        write_json(self.package, {"crs": "EPSG:31254", "layers": [
            {"name": "catchment", "role": "catchment", "path": "catchment.gpkg"},
            {"name": "streams", "role": "streams", "path": "streams.gpkg"}],
            "rasters": [{"name": "dem", "role": "dem", "path": "dem.tif"}]})
        self.form = dict(package=str(self.package), max_output_tokens="24000", mode="vision",
                         review="on", browser_check="on")

    def saved_manifest(self):
        return read_json(self.run / "manifest.json")

    def test_form_and_approval_gate(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Spatial package JSON", page.data)
        self.manifest["status"] = "awaiting_human_review"
        pipeline.save_manifest(self.run, self.manifest)
        response = self.client.post(self.url, data=self.form)
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"Approve this individual report", response.data)
        self.assertFalse(self.executor.tasks)

    def test_queue_duplicate_and_delete_guards(self):
        self.assertEqual(self.client.post(self.url, data=self.form).status_code, 303)
        self.assertEqual(self.saved_manifest()["status"], "approved")
        self.assertEqual(self.saved_manifest()["localization"]["status"], "queued")
        self.assertEqual(self.client.post(self.url, data=self.form).status_code, 409)
        self.assertEqual(len(self.executor.tasks), 1)
        self.assertEqual(self.client.post(self.url.removesuffix('/localization') + '/delete').status_code, 409)
        state = self.client.get(self.url + "/status")
        self.assertEqual(state.headers["Cache-Control"], "no-store")
        self.assertTrue(state.json["busy"])

    def test_worker_delivery_and_allowlisted_downloads(self):
        def localize(run, **kwargs):
            self.assertEqual(Path(kwargs["package"]), self.package)
            self.assertTrue(kwargs["vision"])
            self.assertTrue(kwargs["review"])
            kwargs["log"]("model call 3: localization_segment")
            folder = run / "localization/outputs"
            write_json(folder / "segment_results.json", [{"seg_id": "1", "localization_status": "unresolved", "reason": "No spatial evidence"}])
            (folder / "segments_map.html").write_text("<html>Offline map</html>", encoding="utf-8")
            (folder / "example_review.zip").write_bytes(b"test archive")
            (folder / "private.json").write_text("private", encoding="utf-8")
            return dict(status="delivered_with_unresolved", model_calls=3)

        with patch("multi_hazard_pipeline.agents.localization_agent.localize_run", side_effect=localize):
            self.client.post(self.url, data=self.form)
            self.executor.tasks.pop()()
        self.assertEqual(self.saved_manifest()["status"], "approved")
        self.assertEqual((self.run / "final_rows.json").read_bytes(), self.original_rows)
        state = self.client.get(self.url + "/status").json
        self.assertFalse(state["busy"])
        self.assertEqual(state["info"]["status"], "delivered_with_unresolved")
        self.assertEqual(state["results"][0]["localization_status"], "unresolved")
        self.assertEqual(state["model_calls"], 3)
        for filename in ("segment_results.json", "segments_map.html", "example_review.zip"):
            response = self.client.get(self.url + "/output/" + filename)
            self.assertEqual(response.status_code, 200)
            if filename.endswith("html"):
                self.assertIn("sandbox", response.headers["Content-Security-Policy"])
            response.close()
        for filename in ("private.json", "manifest.json", "checkpoint.json"):
            self.assertEqual(self.client.get(self.url + "/output/" + filename).status_code, 404)

    def test_failure_retains_approved_exports_and_settings(self):
        with patch("multi_hazard_pipeline.agents.localization_agent.localize_run", side_effect=RuntimeError("GIS import failed")):
            self.client.post(self.url, data=self.form)
            self.executor.tasks.pop()()
        manifest = self.saved_manifest()
        self.assertEqual(manifest["status"], "approved")
        self.assertEqual((self.run / "final_rows.json").read_bytes(), self.original_rows)
        self.assertEqual(manifest["localization"]["status"], "failed")
        self.assertIn("GIS import failed", manifest["localization"]["error"])
        self.assertEqual(manifest["localization"]["settings"]["package"], str(self.package))

    def test_existing_folder_requires_resume_and_forwards_options(self):
        write_json(self.run / "localization/llm/checkpoint.json", {"answers": {"1": {}}, "model_calls": 4})
        self.assertEqual(self.client.post(self.url, data=self.form).status_code, 409)
        figure = self.root / "figures.json"
        write_json(figure, {"outlines": []})
        form = {**self.form, "resume": "on", "mode": "text", "allow_network": "on",
                "figure_manifest": str(figure), "only": "1, 2", "model": "test-model"}
        with patch("multi_hazard_pipeline.agents.localization_agent.localize_run", return_value={"status": "partial"}) as localize:
            self.assertEqual(self.client.post(self.url, data=form).status_code, 303)
            self.executor.tasks.pop()()
        options = localize.call_args.kwargs
        self.assertTrue(options["resume"])
        self.assertFalse(options["vision"])
        self.assertTrue(options["allow_network"])
        self.assertEqual(options["only"], ["1", "2"])
        self.assertEqual(options["model"], "test-model")
        self.assertEqual(self.client.get(self.url + "/status").json["processed"], 1)

    def test_invalid_package_and_limits_do_not_queue(self):
        for changes in ({"package": ""}, {"package": str(self.root / "missing.json")},
                        {"max_output_tokens": "0"}, {"max_output_tokens": "bad"}):
            self.assertEqual(self.client.post(self.url, data={**self.form, **changes}).status_code, 409)
        self.assertFalse(self.executor.tasks)
        self.assertNotIn("localization", self.saved_manifest())

    def test_collection_delete_is_blocked_by_localizing_child(self):
        self.client.post(self.url, data=self.form)
        parent = pipeline.create_run(self.root / "inputs", self.root / "results")
        parent.update(status="split", report_mode="multi", child_runs=[{"run_id": self.run.name, "filename": "report.txt"}])
        pipeline.save_manifest(Path(parent["artifact_dir"]), parent)
        self.assertEqual(self.client.post(f"/runs/{parent['run_id']}/delete").status_code, 409)
        self.assertEqual(self.client.get(f"/runs/{parent['run_id']}/localization").status_code, 404)

    def test_executor_failure_retains_approval(self):
        with patch.object(self.executor, "submit", side_effect=RuntimeError("worker stopped")):
            self.assertEqual(self.client.post(self.url, data=self.form).status_code, 409)
        self.assertEqual(self.saved_manifest()["status"], "approved")
        self.assertEqual(self.saved_manifest()["localization"]["status"], "failed")

    def test_real_localizer_through_interface_and_incompatible_resume(self):
        from test_localization_stage import FakeModel, make_package, make_pipeline_run

        package = make_package(self.root / "synthetic-package")
        approved = make_pipeline_run(self.root / "synthetic-report")
        for name in ("translated.json", "segments.json", "final_rows.json"):
            shutil.copyfile(approved / name, self.run / name)
        original = (self.run / "final_rows.json").read_bytes()
        form = {**self.form, "package": str(package), "mode": "text"}
        form.pop("browser_check")
        with patch("multi_hazard_pipeline.llm.ChatClient.from_config", return_value=FakeModel()):
            self.assertEqual(self.client.post(self.url, data=form).status_code, 303)
            self.executor.tasks.pop()()
        state = self.client.get(self.url + "/status").json
        self.assertEqual(state["info"]["status"], "delivered", state["info"].get("processing_failures"))
        self.assertEqual(len(state["results"]), 3)
        self.assertEqual(state["processed"], 3)
        self.assertTrue(state["map_url"])
        self.assertTrue(any(file["name"].endswith("_review.zip") for file in state["downloads"]))
        self.assertIn(b"delivered", self.client.get(self.url).data)
        result_file = self.run / "localization/outputs/segment_results.json"
        previous = result_file.read_bytes()
        with patch("multi_hazard_pipeline.llm.ChatClient.from_config", return_value=FakeModel()):
            self.client.post(self.url, data={**form, "resume": "on", "model": "changed-model"})
            self.executor.tasks.pop()()
        self.assertEqual(self.saved_manifest()["localization"]["status"], "failed")
        self.assertIn("inputs or settings changed", self.saved_manifest()["localization"]["error"])
        self.assertEqual(result_file.read_bytes(), previous)
        self.assertEqual((self.run / "final_rows.json").read_bytes(), original)
        self.assertEqual(self.saved_manifest()["status"], "approved")

    def test_oversized_upload_names_remain_distinct(self):
        prefix = "report-" * 45
        response = self.client.post("/runs", data={"report_mode": "single", "files": [
            (io.BytesIO(b"one"), prefix + "one.txt"), (io.BytesIO(b"two"), prefix + "two.txt")]})
        self.assertEqual(response.status_code, 303)
        run_id = response.headers["Location"].rsplit("/", 1)[-1]
        inputs = read_json(self.run.parent / run_id / "manifest.json")["inputs"]
        self.assertEqual(len({item["filename"] for item in inputs}), 2)
        self.assertTrue(all(len(item["filename"]) <= 180 for item in inputs))
        self.assertEqual([Path(item["path"]).read_bytes() for item in inputs], [b"one", b"two"])


TABLE = ("Segment\tEvent\tProcess\tGeneralized Category\tInteraction Type\tSediment Transport Phase\n"
         "1\tTestbach\tHeavy rain over the catchment\tTriggering Event\tProcess-topography\tnot applicable\n"
         "2\tTestbach\tErosion in the middle course between hm 2 and hm 12\tMaterial Mobilization\tProcess-topography\tErosion\n"
         "3\tTestbach\tDebris flow damaged the Testbrücke\tImpact on infrastructure\tProcess-structure\tTransportation\n"
         "9\tOtherbach\tRockfall onto the road\tImpact on infrastructure\tProcess-structure\tTransportation\n")


class ProvidedLocalizationTests(TestCase):
    """Standalone localization tests from a supplied report, segment table and GIS data."""

    def setUp(self):
        from test_localization_stage import PIPE_PAGES, make_package

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = resolve_path(self.temp.name)
        self.package = make_package(self.root / "gis")
        self.report = self.root / "own" / "report.txt"
        self.report.parent.mkdir()
        self.report.write_text("\n\n".join(PIPE_PAGES), encoding="utf-8")
        self.results = self.root / "results"
        self.executor = QueuedExecutor()
        self.app = web.create_app(self.results, executor=self.executor)
        self.app.testing = True
        self.client = self.app.test_client()
        self.form = dict(spatial_source="package", package=str(self.package), event="testbach", mode="text",
                         review="on", max_output_tokens="24000")

    def post(self, **changes):
        data = {**self.form, "report_files": [(io.BytesIO(self.report.read_bytes()), "report.txt")],
                "segments_file": (io.BytesIO(TABLE.encode("utf-8")), "All segments.tsv"), **changes}
        return self.client.post("/localization/new", data=data, content_type="multipart/form-data")

    def runs(self):
        return [path.parent for path in self.results.glob("*/manifest.json")]

    def test_form_and_navigation(self):
        page = self.client.get("/localization/new")
        self.assertEqual(page.status_code, 200)
        for text in (b"Map your own segments", b"Separate GIS files", b"Spatial package JSON", b"Translate the report"):
            self.assertIn(text, page.data)
        self.assertIn(b"/localization/new", self.client.get("/").data)

    def test_queue_filters_event_and_skips_review(self):
        response = self.post()
        self.assertEqual(response.status_code, 303, response.data[:2000])
        [run] = self.runs()
        self.assertTrue(response.headers["Location"].endswith(f"/runs/{run.name}/localization"))
        manifest = read_json(run / "manifest.json")
        self.assertEqual((manifest["report_mode"], manifest["status"], manifest["event"]),
                         ("provided_localization", "approved", "Testbach"))
        self.assertEqual(manifest["localization"]["status"], "queued")
        self.assertEqual(manifest["localization"]["settings"]["segments_table"], str(run / "inputs/segments_input.tsv"))
        table = (run / "inputs/segments_input.tsv").read_text(encoding="utf-8")
        self.assertIn("Testbrücke", table)
        self.assertNotIn("Otherbach", table)
        self.assertEqual([row["segment"] for row in read_json(run / "final_rows.json")["rows"]], ["1", "2", "3"])
        self.assertEqual(len(self.executor.tasks), 1)
        detail = self.client.get(f"/runs/{run.name}")
        self.assertEqual(detail.status_code, 302)
        self.assertTrue(detail.headers["Location"].endswith("/localization"))
        page = self.client.get(f"/runs/{run.name}/localization").data
        self.assertIn(b"Provided inputs", page)
        self.assertIn(b"3 segment(s)", page)
        index = self.client.get("/").data
        self.assertIn(b"kind-tag", index)
        self.assertIn(f"/runs/{run.name}/localization".encode(), index)

    def test_refused_inputs_leave_nothing_behind(self):
        cases = [dict(event=""), dict(event="Missing"), dict(package=str(self.root / "missing.json")),
                 dict(segments_file=(io.BytesIO(b"Segment\tEvent\n1\tTestbach\n"), "t.tsv")),
                 dict(segments_file=(io.BytesIO(b"x"), "table.xlsx")), dict(max_output_tokens="1"),
                 dict(spatial_source="files", dem=str(self.root / "gis/dem.tif"))]
        for changes in cases:
            response = self.post(**changes)
            self.assertIn(response.status_code, (400, 409), changes)
            self.assertIn(b"message error", response.data)
        self.assertIn("several events", self.post(event="").data.decode("utf-8"))
        self.assertEqual(self.runs(), [])
        uploads = self.results / ".uploads"
        self.assertFalse(uploads.exists() and any(uploads.iterdir()))
        self.assertFalse(self.executor.tasks)

    def test_separate_files_build_a_package(self):
        gis = self.root / "gis"
        response = self.post(spatial_source="files", package="", dem=str(gis / "dem.tif"),
                             catchment=str(gis / "catchment.gpkg"), streams=str(gis / "streams.gpkg"),
                             bridges=str(gis / "bridges.gpkg"), streams_name_field="NAME")
        self.assertEqual(response.status_code, 303, response.data[:2000])
        [run] = self.runs()
        package = read_json(run / "inputs/spatial_package.json")
        self.assertEqual(package["crs"], "EPSG:31254")
        self.assertEqual({layer["role"]: layer["name_field"] for layer in package["layers"]},
                         {"catchment": "NAME", "streams": "NAME", "bridges": "NAME"})
        self.assertEqual(Path(package["rasters"][0]["path"]), gis / "dem.tif")
        self.assertEqual(read_json(run / "manifest.json")["localization"]["package"], str(run / "inputs/spatial_package.json"))

    def test_worker_reads_report_and_localizes(self):
        from test_localization_stage import FakeModel

        links = {"1": [{"page": 1, "quote": "Das Einzugsgebiet des Testbaches umfasst 2,5 km²", "kind": "extent", "note": ""}],
                 "2": [{"page": 1, "quote": "zwischen hm 2 und hm 12 kam es zu Erosion", "kind": "place", "note": ""}],
                 "3": [{"page": 1, "quote": "Der Murgang erreichte die Testbrücke bei hm 4", "kind": "place", "note": ""}]}
        self.assertEqual(self.post().status_code, 303)
        [run] = self.runs()
        with patch("multi_hazard_pipeline.llm.ChatClient.from_config", return_value=FakeModel(links)):
            self.executor.tasks.pop()()
        state = self.client.get(f"/runs/{run.name}/localization/status").json
        self.assertEqual(state["info"]["status"], "delivered", state["info"].get("processing_failures"))
        self.assertEqual((state["processed"], state["total"]), (3, 3))
        self.assertEqual([stage["status"] for stage in state["provided"]["stages"]], ["completed", "skipped", "completed"])
        self.assertTrue((run / "source.json").is_file())
        self.assertTrue(state["map_url"])
        # Relaunching reuses the saved report text and resumes the saved decisions.
        form = {**self.form, "resume": "on", "segments_table": str(run / "inputs/segments_input.tsv")}
        with patch("multi_hazard_pipeline.llm.ChatClient.from_config", return_value=FakeModel(links)):
            self.assertEqual(self.client.post(f"/runs/{run.name}/localization", data=form).status_code, 303)
            self.executor.tasks.pop()()
        self.assertEqual(read_json(run / "manifest.json")["localization"]["status"], "delivered")
        self.assertEqual(self.client.post(f"/runs/{run.name}/delete").status_code, 303)
        self.assertEqual(self.runs(), [])
        self.assertTrue(self.report.is_file() and self.package.is_file())


if __name__ == "__main__":
    main()

