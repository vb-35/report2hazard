"""The interactive map must embed valid WGS84 geometries and keep its legend usable on small screens."""
import json
import re
import tempfile
from pathlib import Path
from unittest import TestCase

from shapely.geometry import LineString, MultiPolygon, Polygon, box, shape

from segment_mapping.build import build
from segment_mapping.deliver import make_map
from segment_mapping.runctx import write_json
from segment_mapping.webmap import clean_geometry, embedded_counts, invalid_embedded, web_geometries
from test_segment_mapping import CRS, _make_run, _spec

# Valid in EPSG:31254 but nearly zero-width: collapses to an invalid ring in longitude/latitude.
HOLE = [(65629.56480485522, 227318.0768897049), (65629.57157442997, 227317.46399825718),
        (65629.57157442998, 227317.46399825718)]


# Valid in EPSG:4326 (as written to segments.geojson) but collapses to a 'too few points' ring in EPSG:31254 and
# after the map's simplify + reprojection round trip (the Seigesbach/Maierhofgraben defect).
WGS_HOLE = [(11.195572238645838, 47.183574955128606), (11.195566738645837, 47.18357495512861),
            (11.195572238645838, 47.1835749551286), (11.195572238645838, 47.183574955128606)]


def _wgs_polygon_with_collapsing_hole():
    return Polygon(box(11.19, 47.18, 11.2, 47.19).exterior.coords, [WGS_HOLE])


def _catchment_with_sliver_hole():
    return Polygon(box(65000, 227000, 66000, 228000).exterior.coords, [HOLE])


def _embedded(page: Path) -> dict:
    m = re.search(r'<script id="segmap-data" type="application/json">(.*?)</script>', page.read_text(encoding="utf-8"), re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


class CleanGeometry(TestCase):
    def test_naive_simplify_and_reprojection_is_invalid_but_web_geometry_is_valid(self):
        import geopandas as gpd

        poly = _catchment_with_sliver_hole()
        self.assertTrue(poly.is_valid)
        naive = gpd.GeoSeries([poly], crs=CRS).simplify(0.3).to_crs(4326).iloc[0]
        self.assertFalse(naive.is_valid)  # the defect seen in the overnight maps
        (web,) = web_geometries([poly], CRS, 0.3)
        self.assertIsNotNone(web)
        self.assertTrue(web.is_valid)
        self.assertEqual(web.geom_type, "Polygon")
        back = gpd.GeoSeries([web], crs=4326).to_crs(CRS).iloc[0]
        self.assertAlmostEqual(back.area, poly.area, delta=poly.area * 1e-4)

    def test_canonical_wgs84_hole_collapsing_in_run_crs_is_repaired(self):
        import geopandas as gpd

        poly = _wgs_polygon_with_collapsing_hole()
        self.assertTrue(poly.is_valid)
        projected = gpd.GeoSeries([poly], crs=4326).to_crs(CRS)
        self.assertFalse(projected.iloc[0].is_valid)
        self.assertFalse(projected.simplify(0.3).to_crs(4326).iloc[0].is_valid)
        (web,) = web_geometries(projected, CRS, 0.3, fallback=[poly])
        self.assertTrue(web.is_valid)
        self.assertEqual(web.geom_type, "Polygon")
        self.assertAlmostEqual(web.area, poly.area, delta=poly.area * 1e-6)

    def test_collapsed_remnants_and_lower_dimension_parts_are_dropped(self):
        # self-touching bow-tie plus a zero-area spike: make_valid alone would leave lines
        bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10), (0, 0)])
        fixed = clean_geometry(bowtie)
        self.assertTrue(fixed.is_valid)
        self.assertIn(fixed.geom_type, ("Polygon", "MultiPolygon"))
        self.assertAlmostEqual(fixed.area, 50)
        spike = Polygon([(0, 0), (10, 0), (10, 10), (10, 20), (10, 10), (0, 10)])
        fixed = clean_geometry(spike)
        self.assertTrue(fixed.is_valid)
        self.assertEqual(fixed.geom_type, "Polygon")
        self.assertAlmostEqual(fixed.area, 100)
        self.assertTrue(clean_geometry(Polygon([(0, 0), (1, 1), (2, 2), (0, 0)])).is_empty)

    def test_lines_and_multipolygons_keep_their_dimension(self):
        line = LineString([(0, 0), (5, 5), (10, 0)])
        self.assertTrue(clean_geometry(line).equals(line))
        mp = MultiPolygon([box(0, 0, 1, 1), box(2, 2, 3, 3)])
        self.assertEqual(clean_geometry(mp).geom_type, "MultiPolygon")
        (web_line,) = web_geometries([LineString([(65000, 227000), (65500, 227600)])], CRS, 0.3)
        self.assertEqual(web_line.geom_type, "LineString")

    def test_fallback_and_invalid_embedded(self):
        (fb,) = web_geometries([Polygon([(0, 0), (1, 1), (2, 2), (0, 0)])], CRS, 0.3, fallback=[box(11, 47, 11.001, 47.001)])
        self.assertTrue(fb.is_valid)
        data = {"segments": [{"features": {"features": [
            {"properties": {"part_id": "x"}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}}]}}],
            "context": [{"label": "c", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}]}
        self.assertEqual(invalid_embedded(data), ["x"])


class MapExport(TestCase):
    def setUp(self):
        import geopandas as gpd

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = _make_run(Path(self.tmp.name))
        self.run.write_layer("catchments", gpd.GeoDataFrame(geometry=[_catchment_with_sliver_hole()], crs=self.run.crs))
        spec = _spec()
        spec.pop("georef")
        spec.pop("visuals")
        spec["parts"] = [spec["parts"][0]]
        spec["map"] = {"context": [{"geom": "catch", "label": "Catchment (context)", "color": "#555"},
                                   {"geom": "bach", "label": "Main stream"}]}
        long_reason = "No spatial evidence in the report for this process. " * 25
        spec["segment_results"] = [dict(seg_id=s["seg_id"], localization_status="located" if s["seg_id"] == "1" else "unresolved",
                                        reason="Identified feature" if s["seg_id"] == "1" else long_reason,
                                        event_confidence="high", location_confidence="medium" if s["seg_id"] == "1" else "unknown",
                                        boundary_confidence="unknown") for s in self.run.segments]
        write_json(self.run.dir / "scripts/placements.json", spec)
        self.assertEqual(build(self.run)["errors"], [])
        self.page = make_map(self.run)

    def test_embedded_features_and_context_are_valid(self):
        data = _embedded(self.page)
        feats = [f for s in data["segments"] for f in s["features"]["features"]]
        self.assertEqual(len(feats), 1)
        self.assertEqual(len(data["context"]), 2)
        for gj in [f["geometry"] for f in feats] + [c["geometry"] for c in data["context"]]:
            g = shape(gj)
            self.assertTrue(g.is_valid, gj["type"])
            self.assertFalse(g.is_empty)
        self.assertEqual(shape(feats[0]["geometry"]).geom_type, "Polygon")
        self.assertEqual({shape(c["geometry"]).geom_type for c in data["context"]}, {"Polygon", "LineString"})
        counts = embedded_counts(self.page.read_text(encoding="utf-8"))
        self.assertEqual(counts["invalid_geometries"], [])
        self.assertEqual(counts["context_layers"], 2)

    def test_canonical_feature_collapsing_in_run_crs_is_embedded_valid(self):
        from shapely.geometry import mapping

        path = self.run.out / "segments.geojson"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["features"][0]["geometry"] = mapping(_wgs_polygon_with_collapsing_hole())
        path.write_text(json.dumps(data), encoding="utf-8")
        page = make_map(self.run)
        feats = [f for s in _embedded(page)["segments"] for f in s["features"]["features"]]
        g = shape(feats[0]["geometry"])
        self.assertTrue(g.is_valid)
        self.assertEqual(g.geom_type, "Polygon")
        self.assertEqual(embedded_counts(page.read_text(encoding="utf-8"))["invalid_geometries"], [])

    def test_legend_is_scrollable_and_collapsible(self):
        page = self.page.read_text(encoding="utf-8")
        css = re.search(r"\.legend\{[^}]*\}", page).group(0)
        self.assertIn("max-height:calc(100vh", css)
        self.assertIn("overflow:auto", css)
        self.assertIn("max-width:min(", css)
        self.assertIn('<details class="lg-main"', page)
        self.assertIn('<details class="lg-unres">', page)  # long unresolved reasons are folded by default
        self.assertIn("disableScrollPropagation(d)", page)
        data = _embedded(self.page)
        self.assertEqual(sum(r["localization_status"] == "unresolved" for r in data["segment_results"]), 2)
