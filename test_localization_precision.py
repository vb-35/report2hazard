"""Candidate dedupe, derived precision tiers and the catchment rule for channel processes."""
import unittest

from shapely.geometry import LineString, Point, Polygon

from multi_hazard_pipeline.agents.localization_agent import catchment_wide
from segment_mapping.build import part_precision, segment_precision
from segment_mapping.facts import _same_feature


class SameFeatureTests(unittest.TestCase):
    def test_copies_of_one_stream_are_one_feature(self):
        line = LineString([(0, 0), (500, 0), (900, 300)])
        copy = LineString([(0, 1), (500, 1), (900, 301)])
        self.assertTrue(_same_feature(line, copy))

    def test_different_geometries_or_types_stay_separate(self):
        line = LineString([(0, 0), (500, 0)])
        self.assertFalse(_same_feature(line, LineString([(0, 0), (300, 0)])))
        self.assertFalse(_same_feature(line, LineString([(0, 50), (500, 50)])))
        self.assertFalse(_same_feature(Polygon([(0, 0), (1, 0), (1, 1)]), line))
        self.assertTrue(_same_feature(Point(3, 3), Point(4, 4)))


class PrecisionTests(unittest.TestCase):
    def test_tiers_follow_the_method(self):
        reach = {"method": "reach_corridor", "method_args": {"from_point": "hm:6", "to_point": "hm:32"}}
        whole = {"method": "reach_corridor", "method_args": {"from_point": "mouth", "to_point": "source"}}
        self.assertEqual(part_precision(reach), "specific")
        self.assertEqual(part_precision(whole), "approximate")
        self.assertEqual(part_precision({"method": "valley_floor"}), "approximate")
        self.assertEqual(part_precision({"method": "feature_geometry", "geometry_role": "location"}), "specific")
        self.assertEqual(part_precision({"method": "catchment"}), "broad")
        self.assertEqual(part_precision({"method": "feature", "geometry_role": "search_area"}), "broad")
        self.assertEqual(part_precision({"placement": "documented"}), "specific")
        self.assertEqual(part_precision({"method": "named_area", "geometry_role": "search_area"}), "approximate")
        self.assertEqual(part_precision({"method": "buildings_near"}), "approximate")

    def test_segment_takes_its_most_specific_part(self):
        self.assertEqual(segment_precision([{"method": "catchment"}, {"method": "valley_floor"}]), "approximate")
        self.assertEqual(segment_precision([]), "unresolved")


class NamedAreaTests(unittest.TestCase):
    def setUp(self):
        from segment_mapping.templates import Compiler

        facts = {"candidates": [{"id": "S1", "kind": "streams", "name": "A", "expr": {}, "layer": "s"},
                                {"id": "C1", "kind": "catchment", "name": "A", "expr": {}, "layer": "c"},
                                {"id": "P1", "kind": "names", "name": "X", "expr": {}, "geom_type": "Point", "layer": "n"}]}
        self.comp = Compiler(facts, {"main_stream": "S1", "catchment": "C1", "named_features": []}, {}, chainage=False)

    def test_named_area_is_always_a_search_area_without_boundary(self):
        cp = self.comp.compile_part("8", "8a", {"method": "named_area", "args": {"center": "P1", "radius_m": 150},
                                                "is_hypo": 1, "confidence": "medium", "geometry_role": "event_extent",
                                                "boundary_confidence": "low"})
        self.assertEqual((cp["geometry_role"], cp["boundary_confidence"], cp["placement"]), ("search_area", "unknown", "approximate"))
        self.assertEqual(cp["geometry"], {"buffer": {"geom": {"representative_point": "P1"}, "distance": 150.0}})
        self.assertEqual(part_precision(cp), "approximate")

    def test_named_area_radius_is_bounded(self):
        from segment_mapping.templates import TemplateError

        for r in (None, 20, 900):
            with self.assertRaises(TemplateError):
                self.comp.compile_part("8", "8a", {"method": "named_area", "args": {"center": "P1", "radius_m": r}})


class ConsistencyCheckTests(unittest.TestCase):
    def test_overprecision_totals_and_unmapped_streams_are_flagged(self):
        from types import SimpleNamespace

        from multi_hazard_pipeline.agents.localization_agent import Localizer

        loc = Localizer.__new__(Localizer)
        loc.chain_info = None
        loc.frame = {"main_stream": "S1", "receiving_river": "S2"}
        loc.facts = {"candidates": [{"id": "S1", "kind": "streams", "name": "Moosbach"},
                                    {"id": "S2", "kind": "streams", "name": "Talache", "aliases": ["Tal-Ache"]},
                                    {"id": "S3", "kind": "streams", "name": "Grubenbach"}]}
        loc.run = SimpleNamespace(segments=[{"seg_id": "8", "category": "Negative Impact"}])
        loc._segment_evidence = lambda sid: []
        loc.parts = {"8": [
            {"part_id": "8a", "method": "location_point", "method_args": {"center": "P4"},
             "quotes": [{"text": "Bewohner im Bereich Oberhof konnten ihre Häuser nicht verlassen"}]},
            {"part_id": "8b", "method": "location_point", "method_args": {"center": "P1"},
             "quotes": [{"text": "Insgesamt waren 12 Gebäude betroffen."}]},
            {"part_id": "8c", "method": "reach_corridor", "method_args": {"from_point": "B2", "to_point": "mouth"},
             "quotes": [{"text": "Das Bachbett des Moosbaches und der Talache wurde verfüllt"}]}]}
        checks = " | ".join(loc._consistency_checks("8", []))
        self.assertIn("8a: the quote gives this place only as an area ('im Bereich')", checks)
        self.assertIn("8b: the quote states a total ('Insgesamt')", checks)
        self.assertIn("not used by any part: S2 Talache", checks)
        self.assertNotIn("S1", checks.split("not used by any part:")[1])
        self.assertNotIn("S3", checks)


class CatchmentRuleTests(unittest.TestCase):
    def test_only_catchment_wide_processes_may_use_the_catchment(self):
        self.assertTrue(catchment_wide({"category": "Triggering Event "}))
        self.assertTrue(catchment_wide({"category": "Unstable pre-event conditions"}))
        self.assertTrue(catchment_wide({"category": "Material Mobilization", "process": "Numerous shallow landslides"}))
        self.assertFalse(catchment_wide({"category": "Material Mobilization", "process": "Strong erosion in the main channel"}))
        self.assertFalse(catchment_wide({"category": "Sediment surge", "process": "Small debris flow within the catchment"}))
        self.assertFalse(catchment_wide({"category": "Negative Impact on permanent or temporary infrastructure"}))


if __name__ == "__main__":
    unittest.main()
