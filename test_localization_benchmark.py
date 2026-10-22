"""Benchmark scorer: candidate segment geometries against reference runs."""
import json
import tempfile
from pathlib import Path
from unittest import TestCase

import geopandas as gpd
from shapely.geometry import LineString, Point, box

from segment_mapping.benchmark import Layer, benchmark, coverage, footprint, parse_named

CRS = "EPSG:31254"
X, Y = 70000, 230000


def _write(folder: Path, rows, crs=CRS, results=None, name="segments.geojson"):
    folder.mkdir(parents=True, exist_ok=True)
    gdf = gpd.GeoDataFrame([dict(seg_id=s, part_id=p, geometry=g) for s, p, g in rows], geometry="geometry", crs=CRS)
    gdf.to_crs(crs).to_file(folder / name, driver="GeoJSON" if name.endswith("json") else None)
    if results is not None:
        (folder / "segment_results.json").write_text(json.dumps([dict(seg_id=s, localization_status=st) for s, st in results.items()]))
    return folder / name


class BenchmarkScoring(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.site_a = box(X, Y, X + 200, Y + 100)
        self.site_b = box(X + 2000, Y, X + 2200, Y + 100)
        ref_rows = [("1", "1a", self.site_a), ("2", "2a", self.site_a), ("2", "2b", self.site_b), ("3", "3a", self.site_a)]
        self.refs = [(f"m{i}", str(_write(self.d / f"ref{i}" / "outputs", ref_rows, name="segments.shp"))) for i in range(3)]

    def run_bench(self, rows, results=None, crs=CRS):
        path = _write(self.d / "cand", rows, crs, results)
        return benchmark(self.refs, [("cand", str(path))], self.d / "out")

    def test_perfect_match(self):
        res = self.run_bench([("1", "1a", self.site_a), ("2", "2a", self.site_a), ("2", "2b", self.site_b), ("3", "3a", self.site_a)])
        r = res["runs"]["cand"]
        self.assertEqual(r["summary"]["score"], 1.0)
        for s in r["segments"].values():
            self.assertEqual((s["precision"], s["recall"], s["iou"], s["site_recall"]), (1.0, 1.0, 1.0, 1.0))
            self.assertFalse(s["scale_mismatch"])
        self.assertEqual(res["ceiling"]["score"], 1.0)
        self.assertTrue((self.d / "out" / "benchmark.md").read_text(encoding="utf-8").startswith("# Localization benchmark"))
        self.assertIn("ceiling", json.loads((self.d / "out" / "benchmark.json").read_text(encoding="utf-8")))

    def test_unresolved_and_missing_score_zero(self):
        res = self.run_bench([("1", "1a", self.site_a)], results={"1": "located", "2": "unresolved"})
        segs, summ = res["runs"]["cand"]["segments"], res["runs"]["cand"]["summary"]
        self.assertEqual((segs["2"]["status"], segs["3"]["status"]), ("unresolved", "missing"))
        self.assertEqual((segs["2"]["score"], segs["3"]["score"]), (0.0, 0.0))
        self.assertEqual((summ["located"], summ["unresolved"], summ["missing"]), (1, 1, 1))
        self.assertAlmostEqual(summ["score"], 1 / 3, places=3)

    def test_empty_geojson_uses_outcome_ledger(self):
        folder = self.d / "empty"
        folder.mkdir()
        layer = folder / "segments.geojson"
        layer.write_text(json.dumps(dict(type="FeatureCollection", features=[])))
        (folder / "segment_results.json").write_text(json.dumps([
            dict(seg_id=str(s), localization_status="unresolved") for s in (1, 2, 3)]))
        result = benchmark(self.refs, [("empty", str(layer))])["runs"]["empty"]
        self.assertEqual(result["summary"]["score"], 0.0)
        self.assertEqual(result["summary"]["unresolved"], 3)
        self.assertTrue(all(s["status"] == "unresolved" for s in result["segments"].values()))

    def test_reference_crs_requires_metre_axes(self):
        layer = self.d / "feet.geojson"
        gpd.GeoDataFrame(dict(seg_id=["1"]), geometry=[box(1000000, 200000, 1000100, 200100)],
                         crs="EPSG:2263").to_file(layer, driver="GeoJSON")
        with self.assertRaisesRegex(ValueError, "metre axes"):
            Layer("feet", layer)

    def test_mixed_geometry_types_are_buffered(self):
        line = LineString([(X + 20, Y + 50), (X + 180, Y + 50)])
        res = self.run_bench([("1", "1a", line), ("1", "1b", Point(X + 100, Y + 50)), ("3", "3a", box(X, Y, X + 4000, Y + 4000))])
        s1, s3 = res["runs"]["cand"]["segments"]["1"], res["runs"]["cand"]["segments"]["3"]
        self.assertEqual((s1["status"], s1["n_parts"]), ("located", 2))
        self.assertEqual(s1["precision"], 1.0)
        self.assertGreater(s1["recall"], 0.99)
        self.assertTrue(s3["scale_mismatch"])
        self.assertLess(s3["precision"], 0.05)
        self.assertGreater(footprint([Point(0, 0)]).area, 1900)  # 25 m buffer ~ 1963 m2

    def test_wgs84_candidate_is_reprojected(self):
        res = self.run_bench([("1", "1a", self.site_a)], crs="EPSG:4326")
        s = res["runs"]["cand"]["segments"]["1"]
        self.assertGreater(s["iou"], 0.99)
        self.assertLess(s["centroid_m"], 1.0)

    def test_site_recall_counts_supported_sites(self):
        res = self.run_bench([("2", "2a", self.site_a.buffer(-20))])
        s = res["runs"]["cand"]["segments"]["2"]
        self.assertEqual((s["sites_hit"], s["n_sites"], s["site_recall"]), (1, 2, 0.5))
        self.assertEqual(s["precision"], 1.0)
        self.assertLess(s["recall"], 0.6)
        # a place mapped by a single reference model is not a site
        _write(self.d / "ref0" / "outputs", [("2", "2a", self.site_a), ("2", "2c", box(X, Y + 3000, X + 50, Y + 3050))], name="segments.shp")
        res = self.run_bench([("2", "2a", box(X, Y + 3000, X + 50, Y + 3050))])
        s = res["runs"]["cand"]["segments"]["2"]
        self.assertEqual((s["n_sites"], s["sites_hit"]), (2, 0))

    def test_search_areas_find_places_without_counting_as_extent(self):
        hit, miss = Point(X + 100, Y + 50).buffer(200), Point(X + 900, Y + 2000).buffer(200)
        gdf = gpd.GeoDataFrame([dict(seg_id="1", part_id="1a", geometry_role="search_area", geometry=hit),
                                dict(seg_id="1", part_id="1b", geometry_role="search_area", geometry=miss),
                                dict(seg_id="3", part_id="3a", geometry_role="event_extent", geometry=self.site_a)],
                               geometry="geometry", crs=CRS)
        (self.d / "cand").mkdir()
        gdf.to_file(self.d / "cand" / "segments.geojson", driver="GeoJSON")
        res = benchmark(self.refs, [("cand", str(self.d / "cand" / "segments.geojson"))])
        s1, s3 = res["runs"]["cand"]["segments"]["1"], res["runs"]["cand"]["segments"]["3"]
        self.assertEqual((s1["precision"], s1["site_recall"], s1["recall"]), (0.5, 1.0, 1.0))  # one of two search areas holds the place
        self.assertEqual((s1["area_ratio"], s1["iou"], s1["scale_mismatch"], s1["search_areas"]), (None, None, False, 2))
        self.assertEqual(s3["score"], 1.0)

    def test_consensus_majority_and_named_specs(self):
        a, b, c = box(0, 0, 10, 10), box(5, 0, 15, 10), box(100, 100, 110, 110)
        self.assertAlmostEqual(coverage([a, b, c], 2).area, 50)
        self.assertAlmostEqual(coverage([a, b, c], 1).area, 250)
        self.assertEqual(parse_named(f"x={self.refs[0][1]}"), ("x", self.refs[0][1]))
        self.assertEqual(parse_named(str(self.d / "ref1"))[0], "ref1")
