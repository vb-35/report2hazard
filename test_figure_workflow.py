"""Offline controlled figure QA: accuracy gates and native geometry preservation."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape

from segment_mapping.figure_workflow import (
    assess_georeferencing, list_prepared_figures, load_prepared_figure,
    prepare_figure, transform_digitized_outline, write_inspection,
)
from segment_mapping.georef import Georef


LIMITS = {"max_check_error_m": 2, "min_control_coverage": .8,
          "min_checkpoint_span": .4, "min_checkpoints": 2}


def candidate():
    def control(x, y):
        return {"pixel": [x, y], "target": [500000 + 2*x, 5200000 - 2*y], "source": "independently surveyed synthetic feature"}
    return {"id": "synthetic", "source": {"report": "synthetic.pdf", "pdf_page": 1, "figure": "1"},
            "geometry_role": "event_extent", "event_date": "unknown", "legend": "Synthetic red outline",
            "mapping_basis": "grid", "control_provenance": "Synthetic known metre grid; no reprojection",
            "crs_evidence": {"status": "verified", "crs": "EPSG:32632", "source": "Synthetic map CRS declaration"},
            "image_size": [100, 100],
            "georef": {"method": "affine", "target_crs": "EPSG:32632", "grid_crs": "EPSG:32632",
                       "gcps": [control(0, 0), control(100, 0), control(100, 100), control(0, 100)],
                       "checks": [control(20, 20), control(80, 80)]}}


INTERPRETATION = {"geometry_role": "event_extent", "event_date": "unknown",
                  "legend": "Red process outline; temporal relevance unverified",
                  "reason": "Synthetic test only", "event_confidence": "unknown", "boundary_confidence": "low"}


class FigureWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        Image.new("RGB", (100, 100), "white").save(self.root / "source.png")

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, data=None):
        return prepare_figure(self.root / "source.png", self.root / "candidate", data or candidate())

    def test_affine_rejects_degenerate_source_and_target(self):
        cfg = candidate()["georef"]
        for field in ("pixel", "target"):
            controls = copy.deepcopy(cfg["gcps"])
            for i, c in enumerate(controls):
                c[field] = [i, 2*i]
            with self.assertRaisesRegex(ValueError, "degenerate"):
                Georef("bad", cfg, controls, [])
        controls[0]["pixel"] = [float("nan"), 1]
        with self.assertRaisesRegex(ValueError, "finite"):
            Georef("bad", cfg, controls, [])

    def test_exact_fit_is_not_accuracy_validation(self):
        data = candidate()
        data["georef"]["gcps"] = data["georef"]["gcps"][:3]
        data["georef"]["checks"] = []
        qa = assess_georeferencing(data, LIMITS)
        self.assertFalse(qa["accepted"])
        self.assertLess(qa["georef"]["rms_m"], .001)
        self.assertIn("Exactly determined", qa["georef"]["note"])
        self.assertIsNone(qa["max_checkpoint_error_m"])

    def test_checkpoint_error_and_reuse_and_coverage_gates(self):
        data = candidate()
        self.assertTrue(assess_georeferencing(data, LIMITS)["accepted"])
        data["georef"]["checks"][0]["target"][0] += 5
        self.assertFalse(assess_georeferencing(data, LIMITS)["accepted"])
        data = candidate()
        data["georef"]["checks"][0] = copy.deepcopy(data["georef"]["gcps"][0])
        self.assertIn("overlap", " ".join(assess_georeferencing(data, LIMITS)["reasons"]))
        data = candidate()
        data["georef"]["gcps"] = [{**c, "pixel": [v*.1 for v in c["pixel"]]} for c in data["georef"]["gcps"]]
        self.assertIn("Control coverage", " ".join(assess_georeferencing(data, LIMITS)["reasons"]))

    def test_crs_unknown_never_promoted_by_low_error(self):
        data = candidate()
        data["crs_evidence"] = {"status": "unknown", "hypothesis": "EPSG:32632"}
        self.assertFalse(assess_georeferencing(data, LIMITS)["accepted"])
        data["mapping_basis"] = "landmarks"
        self.assertTrue(assess_georeferencing(data, LIMITS)["accepted"])
        data["georef"]["target_crs"] = "EPSG:4326"
        self.assertIn("metres", " ".join(assess_georeferencing(data, LIMITS)["reasons"]))

    def test_native_geometries_holes_and_reprojection(self):
        path = self.prepare()
        polygon = Polygon([(10,10), (40,10), (40,40), (10,40)], [[(20,20), (20,30), (30,30), (30,20)]])
        geometries = [Point(20,30), LineString([(10,10),(60,60)]), polygon,
                      MultiPolygon([polygon, Polygon([(60,60),(80,60),(80,80),(60,80)])])]
        for geom in geometries:
            output = transform_digitized_outline(path, mapping(geom), limits=LIMITS, interpretation=INTERPRETATION)
            result = shape(output["geometry"])
            self.assertEqual(result.geom_type, geom.geom_type)
            self.assertAlmostEqual(result.area, geom.area*4, places=5)
            self.assertEqual(output["properties"]["boundary_confidence"], "low")
            self.assertNotIn("documented", output["properties"])
        self.assertEqual(len(shape(transform_digitized_outline(path, mapping(polygon), limits=LIMITS,
                         interpretation=INTERPRETATION)["geometry"]).interiors), 1)
        wgs = transform_digitized_outline(path, mapping(Point(20,30)), limits=LIMITS,
                                         interpretation=INTERPRETATION, target_crs="EPSG:4326")
        self.assertEqual(wgs["target_crs"], "EPSG:4326")
        self.assertTrue(8 < shape(wgs["geometry"]).x < 10)
        self.assertTrue(46 < shape(wgs["geometry"]).y < 48)

    def test_inspection_manifest_and_hash_integrity(self):
        data = candidate()
        original = copy.deepcopy(data)
        path = self.prepare(data)
        qa = json.loads(write_inspection(path, LIMITS).read_text(encoding="utf-8"))
        self.assertTrue(qa["accepted"])
        self.assertTrue((path.parent / "controls_inspection.png").exists())
        self.assertEqual(data, original)
        self.assertEqual(list_prepared_figures(self.root)[0]["id"], "synthetic")
        Image.new("RGB", (100,100), "black").save(path.parent / "figure.png")
        with self.assertRaisesRegex(ValueError, "hash"):
            load_prepared_figure(path)

    def test_path_traversal_and_extrapolation_rejected(self):
        path = self.prepare()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["image"] = "../source.png"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inside"):
            load_prepared_figure(path)
        data = candidate()
        data["georef"]["gcps"] = data["georef"]["gcps"][:3]
        path = self.prepare(data)
        limits = {**LIMITS, "min_control_coverage": .4}
        with self.assertRaisesRegex(ValueError, "extrapolates"):
            transform_digitized_outline(path, mapping(Point(10,90)), limits=limits, interpretation=INTERPRETATION)


if __name__ == "__main__":
    unittest.main()
