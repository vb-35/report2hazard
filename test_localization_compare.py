"""Comparing localizations: pipeline runs and imported agent runs of the same report."""
import csv
import io
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest import TestCase

from test_localization_stage import CRS, FakeModel, make_package, make_pipeline_run


def agent_zip(rows=None, results=None, drop=None) -> bytes:
    """An agent's outputs in the import format: parts for segments 1 and 2, segment 3 unresolved."""
    import geopandas as gpd
    from shapely.geometry import LineString, box

    tmp = Path(tempfile.mkdtemp())
    try:
        parts = rows or [
            dict(seg_id="1", part_id="1a", label="Catchment", geometry_role="search_area", is_hypo=0, event_confidence="high",
                 location_confidence="medium", boundary_confidence="unknown", method="whole catchment", area_ha=None,
                 report_area_ha=None, geometry=box(600, 100, 1400, 1950)),
            dict(seg_id="2", part_id="2a", label="Middle course", geometry_role="location", is_hypo=1, event_confidence="medium",
                 location_confidence="low", boundary_confidence="unknown", method="hm 2-12 on the stream", area_ha=None,
                 report_area_ha=None, geometry=LineString([(1000, 300), (1000, 1300)])),
        ]
        gpd.GeoDataFrame(parts, crs=CRS).to_file(tmp / "placements.gpkg", layer="parts")
        with open(tmp / "segment_results.csv", "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["seg_id", "status", "parts", "reason"])
            writer.writerows(results or [["1", "located", 1, "rain over the catchment"], ["2", "located", 1, "stations"],
                                         ["3", "unresolved", 0, "bridge not identified"]])
        with open(tmp / "segments_evidence.csv", "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["seg_id", "part_id", "quotes", "reasoning", "uncertainty", "alternatives"])
            writer.writerow(["1", "1a", '"Das Einzugsgebiet des Testbaches umfasst 2,5 km²." (p. 1)', "catchment", "", ""])
            writer.writerow(["2", "2a", '"zwischen hm 2 und hm 12" (p. 3)', "stations", "upper limit", "whole stream"])
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name in ("placements.gpkg", "segment_results.csv", "segments_evidence.csv"):
                if name != drop:
                    archive.write(tmp / name, f"outputs/{name}")
        return buffer.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class LocalizationCompareTests(TestCase):
    @classmethod
    def setUpClass(cls):
        from multi_hazard_pipeline.agents.localization_agent import localize_run
        from multi_hazard_pipeline.core import read_json, write_json

        cls.tmp = Path(tempfile.mkdtemp()).resolve()
        cls.results = cls.tmp / "results"
        cls.results.mkdir()
        pkg = make_package(cls.tmp / "package")
        run = make_pipeline_run(cls.results)
        manifest = read_json(run / "manifest.json")
        manifest.update(run_id=run.name, event="Testbach", created_at="2026-11-10T09:00:00+00:00")
        for item in manifest["inputs"]:  # real runs record the file name the reports list shows
            item.setdefault("filename", Path(item["path"]).name)
        write_json(run / "manifest.json", manifest)
        localize_run(run, pkg, client=FakeModel(), vision=False, browser_check=False, log=lambda m: None)
        cls.base = run.name

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        for path in self.results.iterdir():  # keep only the pipeline run between tests
            if path.name != self.base:
                shutil.rmtree(path, ignore_errors=True)

    def test_pipeline_run_is_comparable_with_its_model(self):
        from multi_hazard_pipeline.localization_compare import comparable_groups

        [group] = comparable_groups(self.results)
        self.assertEqual((group["event"], group["segments"]), ("Testbach", 3))
        [run] = group["runs"]
        self.assertEqual((run["run_id"], run["kind"]), (self.base, "pipeline"))
        self.assertTrue(run["model"].startswith("Pipeline 2026-11-10 09:00 · "))

    def test_imported_agent_run_joins_its_report_and_compares(self):
        from multi_hazard_pipeline.localization_compare import comparable_groups, comparison_data, import_agent_run

        [group] = comparable_groups(self.results)
        run_dir = import_agent_run(self.results, report=group["signature"], model="Claude Opus 5.5 · medium",
                                   data=agent_zip(), filename="opus.zip")
        [group] = comparable_groups(self.results)
        self.assertEqual([r["kind"] for r in group["runs"]], ["pipeline", "agent"])  # pipeline first, then agents
        self.assertEqual({r["kind"] for r in group["runs"]}, {"pipeline", "agent"})
        data = comparison_data(self.results, [self.base, run_dir.name])
        self.assertEqual([m["label"] for m in data["models"]][1], "Claude Opus 5.5 · medium")
        [report] = data["reports"]
        self.assertEqual([s["id"] for s in report["segments"]], ["1", "2", "3"])
        agent = report["runs"][run_dir.name]
        self.assertEqual(agent["results"]["3"]["status"], "unresolved")
        part = next(f for f in agent["features"] if f["properties"]["part"] == "2a")
        self.assertEqual((part["properties"]["role"], part["properties"]["hypo"]), ("location", True))
        self.assertIn("hm 2", part["properties"]["quotes"])
        lon, lat = part["geometry"]["coordinates"][0]
        self.assertTrue(-180 <= lon <= 180 and -90 <= lat <= 90)  # reprojected to WGS84
        pipeline = report["runs"][self.base]
        self.assertTrue(pipeline["features"])
        self.assertEqual(set(pipeline["results"]), {"1", "2", "3"})
        self.assertTrue(report["context"]["catchment"] and report["context"]["streams"])

    def test_import_rejects_incomplete_or_foreign_outputs(self):
        from multi_hazard_pipeline.errors import PipelineError
        from multi_hazard_pipeline.localization_compare import comparable_groups, import_agent_run

        report = comparable_groups(self.results)[0]["signature"]
        cases = {
            "missing segments_evidence.csv": dict(data=agent_zip(drop="segments_evidence.csv")),
            "not in the chosen report": dict(data=agent_zip(results=[["1", "located", 1, ""], ["2", "located", 1, ""],
                                                                      ["3", "unresolved", 0, ""], ["99", "located", 1, ""]])),
            "no row for segments 3": dict(data=agent_zip(results=[["1", "located", 1, ""], ["2", "located", 1, ""]])),
            "not a valid zip": dict(data=b"not a zip"),
        }
        for message, kwargs in cases.items():
            with self.subTest(message), self.assertRaisesRegex(PipelineError, message):
                import_agent_run(self.results, report=report, model="Sol", filename="x.zip", **kwargs)
        with self.assertRaisesRegex(PipelineError, "Choose the report"):
            import_agent_run(self.results, report="unknown", model="Sol", filename="x.zip", data=agent_zip())
        self.assertEqual([p.name for p in self.results.iterdir()], [self.base])  # nothing half-imported is left

    def test_runs_of_different_reports_are_not_compared(self):
        from multi_hazard_pipeline.core import read_json, write_json
        from multi_hazard_pipeline.errors import PipelineError
        from multi_hazard_pipeline.localization_compare import comparison_data

        other = self.results / "other-report"
        shutil.copytree(self.results / self.base, other)
        config = read_json(other / "localization" / "run.json")
        config["segments"] = config["segments"][:1]
        write_json(other / "localization" / "run.json", config)
        with self.assertRaisesRegex(PipelineError, "different reports"):
            comparison_data(self.results, [self.base, other.name])

    def test_report_picker_clears_and_switches_without_removing_runs(self):
        from multi_hazard_pipeline import web
        from multi_hazard_pipeline.core import read_json, write_json
        from multi_hazard_pipeline.localization_compare import comparable_groups

        other = self.results / "other-report"
        shutil.copytree(self.results / self.base, other)
        config_path = other / "localization" / "run.json"
        config = read_json(config_path)
        config["segments"] = config["segments"][:1]
        write_json(config_path, config)
        groups = comparable_groups(self.results)
        self.assertEqual(len(groups), 2)
        client = web.create_app(self.results).test_client()

        picker = client.get("/localization/compare").get_data(as_text=True)
        self.assertIn("Choose a report", picker)
        self.assertIn('href="/localization/import">Open another ZIP', picker)
        self.assertNotIn('id="cmp"', picker)
        for group in groups:
            selected = client.get("/localization/compare", query_string={"report": group["signature"]}).get_data(as_text=True)
            self.assertIn('href="/localization/compare">Clear selection', selected)
            self.assertIn('id="cmp"', selected)
            for run in group["runs"]:
                self.assertIn(f'value="{run["run_id"]}" checked', selected)
            self.assertIn('href="/localization/import">Open another ZIP', selected)
            cleared = client.get("/localization/compare").get_data(as_text=True)
            self.assertNotIn('id="cmp"', cleared)
            self.assertIn(group["signature"], cleared)

        self.assertEqual(len(comparable_groups(self.results)), 2)
        inferred = client.get("/localization/compare", query_string={"runs": self.base}).get_data(as_text=True)
        self.assertIn(f'value="{self.base}" checked', inferred)
        stale = client.get("/localization/compare?report=missing").get_data(as_text=True)
        self.assertNotIn('id="cmp"', stale)

    def test_interface_imports_lists_and_serves_the_comparison(self):
        from multi_hazard_pipeline import web
        from multi_hazard_pipeline.localization_compare import comparable_groups

        report = comparable_groups(self.results)[0]["signature"]
        client = web.create_app(self.results).test_client()
        page = client.get(f"/localization/compare?report={report}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'value="{self.base}" checked', page.get_data(as_text=True))
        self.assertIn(report, client.get("/localization/import").get_data(as_text=True))
        response = client.post("/localization/import", data={
            "report": report, "model": "GPT-6.1 Sol · medium",
            "outputs": (io.BytesIO(agent_zip()), "sol.zip")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        result = response.get_data(as_text=True)
        self.assertIn("1 imported", result)
        self.assertIn(f"/localization/compare?report={report}", result)  # link to the comparison of that report
        agent_id = next(p.name for p in self.results.iterdir() if p.name != self.base)
        data = client.get(f"/localization/compare/data?runs={self.base}&runs={agent_id}")
        self.assertEqual(data.status_code, 200)
        self.assertEqual(len(data.get_json()["models"]), 2)
        viewer = client.get(f"/localization/compare?report={report}").get_data(as_text=True)
        self.assertIn('id="cmp"', viewer)
        self.assertIn(f'value="{agent_id}" checked', viewer)  # every run of the report is shown by default
        only = client.get(f"/localization/compare?report={report}&runs={agent_id}").get_data(as_text=True)
        self.assertNotIn(f'value="{self.base}" checked', only)
        self.assertIn("compare.js", viewer)
        index = client.get("/").get_data(as_text=True)
        self.assertIn("GPT-6.1 Sol · medium", index)
        self.assertIn("Agent run", index)
        self.assertEqual(client.get("/vendor/leaflet/leaflet.js").status_code, 200)
        self.assertEqual(client.get("/vendor/leaflet/other.js").status_code, 404)
        bad = client.get("/localization/compare/data?runs=missing")
        self.assertEqual(bad.status_code, 400)
        refused = client.post("/localization/import", data={"report": report, "model": "x",
                                                            "outputs": (io.BytesIO(b"nope"), "x.zip")},
                              content_type="multipart/form-data")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("not a valid zip", refused.get_data(as_text=True))
        download = client.get(f"/localization/compare/download?runs={self.base}&runs={agent_id}")
        self.assertEqual(download.status_code, 200)
        self.assertIn("bundle.json", zipfile.ZipFile(io.BytesIO(download.data)).namelist())
        self.assertEqual(client.get("/localization/compare/download?runs=missing").status_code, 400)
        deleted = client.post(f"/runs/{agent_id}/delete")
        self.assertEqual(deleted.status_code, 303)
        self.assertFalse((self.results / agent_id).exists())

    def test_terrain_view_serves_the_dem_and_parts_in_metres(self):
        from multi_hazard_pipeline import web
        from multi_hazard_pipeline.localization_compare import comparable_groups, import_runs

        report = comparable_groups(self.results)[0]["signature"]
        [outcome] = import_runs(self.results, agent_zip(), filename="sol.zip", report=report, model="GPT-6.1 Sol")
        client = web.create_app(self.results).test_client()
        data = client.get(f"/localization/terrain?runs={self.base}&runs={outcome['run_id']}").get_json()
        grid = data["terrain"]
        self.assertEqual(len(grid["z"]), grid["nx"] * grid["ny"])
        self.assertLessEqual(max(grid["nx"], grid["ny"]), 200)
        self.assertLess(grid["zmin"], grid["zmax"])
        x0, y0, x1, y1 = grid["bounds"]
        part = next(f for f in data["runs"][outcome["run_id"]]["features"] if f["properties"]["part"] == "2a")
        for x, y in part["geometry"]["coordinates"]:  # back in the run's projected CRS, on the line drawn at x = 1000
            self.assertAlmostEqual(x, 1000, delta=1)
            self.assertTrue(x0 <= x <= x1 and y0 - 1 <= y <= y1 + 1)
        lon, lat = (grid["lonlat_bounds"][0] + grid["lonlat_bounds"][2]) / 2, (grid["lonlat_bounds"][1] + grid["lonlat_bounds"][3]) / 2
        a, b, c, d, e, f = grid["lonlat_to_xy"]
        self.assertTrue(x0 - 50 <= a * lon + b * lat + c <= x1 + 50 and y0 - 50 <= d * lon + e * lat + f <= y1 + 50)
        page = client.get(f"/runs/{self.base}/localization").get_data(as_text=True)
        self.assertIn('id="terrain-box"', page)
        self.assertIn("terrain3d.js", page)
        compare = client.get(f"/localization/compare?report={report}").get_data(as_text=True)
        self.assertIn('data-view="3d"', compare)
        self.assertIn(f"runs={outcome['run_id']}\">Only this run", compare)
        index = client.get("/").get_data(as_text=True)
        self.assertIn(f"report={report}&amp;runs={outcome['run_id']}", index)  # an agent card opens just that run
        self.assertEqual(client.get("/localization/terrain?runs=missing").status_code, 400)

    def test_global_terrain_streams_without_building_a_local_grid(self):
        from unittest.mock import patch
        from multi_hazard_pipeline import web
        from multi_hazard_pipeline.localization_compare import comparison_data
        expected = comparison_data(self.results, [self.base])["reports"][0]
        client = web.create_app(self.results).test_client()
        with patch("multi_hazard_pipeline.localization_compare._terrain_grid") as grid:
            response = client.get(f"/localization/terrain?runs={self.base}&source=online&extent=full")
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data["terrain"]["streaming"])
            self.assertEqual(data["runs"], expected["runs"])  # global renderer needs WGS84
            self.assertEqual(data["context"], expected["context"])
            grid.assert_not_called()
        self.assertEqual(client.get(f"/localization/terrain?runs={self.base}&extent=invalid").status_code, 400)

    def test_full_local_terrain_matches_file_extent_and_warns(self):
        import rasterio
        from rasterio.warp import transform_bounds
        from multi_hazard_pipeline.localization_compare import _terrain_grid, dem_file
        base = self.results / self.base
        grid = _terrain_grid(base, extent="full")
        with rasterio.open(dem_file(base)) as ds:
            expected = transform_bounds(ds.crs, grid["crs"], *ds.bounds)
        self.assertEqual(grid["bounds"], [round(v, 1) for v in expected])
        self.assertIn("finite coverage", grid["notice"])
        self.assertLessEqual(max(grid["nx"], grid["ny"]), 200)

    def test_terrain_sources_without_a_run_dem(self):
        from unittest.mock import patch
        from multi_hazard_pipeline import web
        from multi_hazard_pipeline.localization_compare import dem_file
        from multi_hazard_pipeline.core import read_json
        base = self.results / self.base
        dem = dem_file(base)
        hidden = dem.with_suffix(".backup")
        original_config = (base / "localization/run.json").read_bytes()
        dem.rename(hidden)
        try:
            client = web.create_app(self.results).test_client()
            self.assertIn('id="terrain-box"', client.get(f"/runs/{self.base}/localization").get_data(as_text=True))
            url = f"/localization/terrain?runs={self.base}"
            self.assertEqual(client.get(url).status_code, 400)
            def fake_fetch(path, bounds, crs):
                shutil.copyfile(hidden, path)
            with patch("multi_hazard_pipeline.terrain_sources.fetch_online_dem", side_effect=fake_fetch) as fetch:
                first = client.get(url + "&source=online")
                self.assertEqual(first.status_code, 200, first.get_json())
                self.assertIn("Mapzen Terrain Tiles", first.get_json()["terrain"]["attribution"])
                self.assertEqual(client.get(url + "&source=online").status_code, 200)
                self.assertEqual(fetch.call_count, 1)
            uploaded = client.post(url + "&source=upload", data={"dem": (io.BytesIO(hidden.read_bytes()), "dem.tif")})
            self.assertEqual(uploaded.status_code, 200, uploaded.get_json())
            self.assertEqual(client.get(url + "&source=upload").status_code, 200)
            self.assertEqual(client.get(url + "&source=bogus").status_code, 400)
            self.assertEqual(client.post(url + "&source=upload", data={"dem": (io.BytesIO(b"bad"), "bad.tif")}).status_code, 400)
            self.assertEqual(client.get(url + "&source=upload").status_code, 200)  # failed upload preserves prior DEM
            self.assertEqual((base / "localization/run.json").read_bytes(), original_config)
        finally:
            hidden.rename(dem)
            shutil.rmtree(base / "localization/visualization", ignore_errors=True)

    def test_uploaded_dem_is_reprojected_and_nonoverlap_is_rejected(self):
        import numpy as np
        import rasterio
        from rasterio.io import MemoryFile
        from rasterio.transform import from_bounds
        from rasterio.warp import transform_bounds
        from multi_hazard_pipeline import web
        from multi_hazard_pipeline.localization_compare import dem_file
        base = self.results / self.base
        with rasterio.open(dem_file(base)) as src:
            bounds = transform_bounds(src.crs, 4326, *src.bounds)
        def raster(bounds, crs):
            with MemoryFile() as mem:
                with mem.open(driver="GTiff", width=40, height=40, count=1, dtype="float32", crs=crs,
                              transform=from_bounds(*bounds, 40, 40)) as dst:
                    dst.write(np.arange(1600, dtype="float32").reshape(40, 40), 1)
                return mem.read()
        client = web.create_app(self.results).test_client()
        url = f"/localization/terrain?runs={self.base}&source=upload"
        try:
            response = client.post(url, data={"dem": (io.BytesIO(raster(bounds, "EPSG:4326")), "wgs84.tif")})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()["terrain"]["crs"], CRS)
            self.assertEqual(response.headers["X-Terrain-Version"], "2")
            # A run's original DEM may also use a different CRS: always warp it, never reject or relabel it.
            dem = dem_file(base)
            backup = dem.with_suffix(".backup")
            dem.rename(backup)
            try:
                dem.write_bytes(raster(bounds, "EPSG:4326"))
                run_dem = client.get(f"/localization/terrain?runs={self.base}&source=run")
                self.assertEqual(run_dem.status_code, 200, run_dem.get_json())
                grid = run_dem.get_json()["terrain"]
                self.assertEqual(grid["crs"], CRS)
                self.assertEqual(grid["source"], "run")
                self.assertLess(grid["zmin"], grid["zmax"])
            finally:
                dem.unlink(missing_ok=True)
                backup.rename(dem)
            response = client.post(url, data={"dem": (io.BytesIO(raster((100, 20, 101, 21), "EPSG:4326")), "far.tif")})
            self.assertEqual(response.status_code, 400)
            self.assertIn("does not overlap", response.get_json()["error"])
        finally:
            shutil.rmtree(base / "localization/visualization", ignore_errors=True)

    def test_bundle_round_trip_into_an_empty_workspace(self):
        from multi_hazard_pipeline.localization_compare import comparable_groups, export_runs, import_runs

        report = comparable_groups(self.results)[0]["signature"]
        import_runs(self.results, agent_zip(), filename="opus.zip", report=report, model="Claude Opus 5.5")
        ids = [run["run_id"] for run in comparable_groups(self.results)[0]["runs"]]
        data, name = export_runs(self.results, ids)
        self.assertTrue(name.startswith("testbach_runs_") and name.endswith(".zip"))
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn("bundle.json", names)
        self.assertTrue(any(n.endswith("localization/data_raw/sources.gpkg") for n in names))  # GIS context travels along
        self.assertTrue(any(n.endswith("localization/data_raw/dem.tif") for n in names))  # and the DEM for the 3D view
        other = self.tmp / "other-workspace"
        other.mkdir()
        try:
            outcomes = import_runs(other, data, filename=name)
            self.assertEqual([(o["kind"], o["status"]) for o in outcomes], [("pipeline", "imported"), ("agent", "imported")])
            [group] = comparable_groups(other)
            self.assertEqual((group["signature"], group["runs"][1]["model"]), (report, "Claude Opus 5.5"))
            again = import_runs(other, data, filename=name)
            self.assertEqual({o["status"] for o in again}, {"skipped"})
            self.assertEqual(len(comparable_groups(other)[0]["runs"]), 2)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_zip_of_several_agent_folders_names_runs_after_their_folders(self):
        from multi_hazard_pipeline.localization_compare import comparable_groups, import_runs

        report = comparable_groups(self.results)[0]["signature"]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for folder in ("opus-5-5", "sol-6-1"):
                with zipfile.ZipFile(io.BytesIO(agent_zip())) as one:
                    for member in one.namelist():
                        archive.writestr(f"runs/{folder}/{member}", one.read(member))
        outcomes = import_runs(self.results, buffer.getvalue(), filename="runs.zip", report=report)
        self.assertEqual(sorted(o["label"] for o in outcomes), ["opus-5-5", "sol-6-1"])
        self.assertEqual({o["status"] for o in outcomes}, {"imported"})

    def test_unsafe_or_empty_zips_are_refused(self):
        from multi_hazard_pipeline.errors import PipelineError
        from multi_hazard_pipeline.localization_compare import comparable_groups, import_runs

        report = comparable_groups(self.results)[0]["signature"]
        for name, message in (("../evil/placements.gpkg", "unsafe path"), ("notes.txt", "contains no run")):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr(name, b"x")
            with self.subTest(message), self.assertRaisesRegex(PipelineError, message):
                import_runs(self.results, buffer.getvalue(), filename="x.zip", report=report)
