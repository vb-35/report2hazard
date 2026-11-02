"""Offline tests for the segment_mapping toolkit (synthetic DEM, layers and report text; no network)."""
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest import TestCase

import numpy as np

from segment_mapping.build import build
from segment_mapping.deliver import ZIP_FILES, deliver, validate
from segment_mapping.geom import Chainage, Evaluator
from segment_mapping.georef import Georef, eval_color_rule
from segment_mapping.report import resolve_quote
from segment_mapping.runctx import Run, parse_segment_table, write_json

CRS = "EPSG:31254"
SEGMENTS = (
    "Segment\tEvent\tProcess\tGeneralized Category\tInteraction Type\tSediment Transport Phase\n"
    "1\tTestbach\tHeavy rain\tTriggering event\tProcess-topography\tDysconnectivity\n"
    "2\tTestbach\tDebris flow in channel\tSediment surge\tProcess-topography\tTransportation \n"
    "3\tTestbach\tBridge damaged\tImpact on infrastructure\tProcess-structure\tTransportation\n"
)
PAGES = [
    "Header Report 1\nSeite 10\nDas Einzugsgebiet Testbach umfasst eine Fläche von 2,5 km². Der Murgang erreichte die Brü-\ncke bei hm 4.\n",
    "Header Report 1\nSeite 11\nDie Ablagerungen bedeckten rund 3 ha am Schwemmkegel und der Bach wurde verfüllt, so dass\n",
    "Header Report 1\nSeite 12\ndie Straße auf 50 m unterbrochen wurde.\nAbbildung 3: Luftbild des Kegels\n",
]


def _make_run(root: Path) -> Run:
    import geopandas as gpd
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import LineString, Polygon, box

    run = Run(root)
    run.ensure_dirs()
    segments, header = parse_segment_table(SEGMENTS)
    run.config = dict(event="Testbach test", report=str(root / "missing_report.pdf"), crs=CRS, printed_page_offset=9,
                      segments=segments, segment_header=header, rasters={"dem": "data_raw/dem.tif"}, default_dem="dem",
                      bbox=[0, 0, 2000, 2000])
    run.save_config()
    # DEM: valley along x = 1000 sloping down to y = 0; 10 m cells
    xs = np.arange(0, 2000, 10) + 5
    ys = np.arange(2000, 0, -10) - 5
    X, Y = np.meshgrid(xs, ys)
    z = (1000 + 0.2 * Y + 0.15 * np.abs(X - 1000)).astype("float32")
    with rasterio.open(root / "data_raw/dem.tif", "w", driver="GTiff", width=200, height=200, count=1, dtype="float32",
                       crs=CRS, transform=from_origin(0, 2000, 10, 10)) as ds:
        ds.write(z, 1)
    stream = LineString([(1000, 1900), (1000, 100)])  # drawn from the top: the toolkit must orient it
    run.write_layer("streams", gpd.GeoDataFrame({"NAME": ["Testbach"]}, geometry=[stream], crs=CRS))
    run.write_layer("catchments", gpd.GeoDataFrame({"NAME": ["Testbach EZG"]}, geometry=[Polygon([(600, 100), (1400, 100), (1400, 1950), (600, 1950)])], crs=CRS))
    run.write_layer("bridges", gpd.GeoDataFrame({"NAME": ["Testbrücke"]}, geometry=[LineString([(990, 500), (1010, 500)])], crs=CRS))
    run.write_layer("buildings", gpd.GeoDataFrame({"NAME": ["A", "B"]}, geometry=[box(1100, 300, 1120, 320), box(1200, 400, 1215, 415)], crs=CRS))
    write_json(root / "data_raw/report_text.json", [dict(pdf_page=i + 1, printed_page=i + 10, text=t) for i, t in enumerate(PAGES)])
    write_json(root / "data_raw/report_captions.json", [dict(kind="Abbildung", number="3", pdf_page=3, printed_page=12, caption="Abbildung 3: Luftbild des Kegels")])
    run.record_source(dict(name="streams", kind="vector", provider="synthetic", url="", layer="streams", licence="test", used_for="test", role="streams"))
    return run


def _spec():
    common = dict(evidence_type="text", geom_basis="test basis", data_src="test layers", rep_page="10",
                  reasoning="r", uncertainty="u", reason_placement="p", reason_is_hypo="h", reason_confidence="c")
    return {
        "event": "Testbach test",
        "refs": {
            "bach": {"stream": {"layer": "streams", "where": {"NAME": "Testbach"}}},
            "catch": {"layer": "catchments", "merge": "first"},
            "bridge": {"layer": "bridges", "where": {"NAME": "Testbrücke"}, "merge": "first"},
        },
        "chainages": {"hm": {"line": "bach", "unit_m": 100, "anchors": [{"value": 0, "at": "start"}, {"value": 4, "point": {"centroid": "bridge"}, "label": "bridge"}]}},
        "georef": {"img": {"figure": "3", "image": "tmp/report_inspection/fake.png", "method": "affine",
                           "gcps": [{"label": "a", "pixel": [0, 0], "target": [900, 700]}, {"label": "b", "pixel": [100, 0], "target": [1100, 700]},
                                    {"label": "c", "pixel": [0, 100], "target": [900, 500]}, {"label": "d", "pixel": [100, 100], "target": [1100, 500]}],
                           "checks": [{"label": "e", "pixel": [50, 50], "target": [1000, 600]}]}},
        "visuals": [{"figure": "3", "evidence_type": "orthophoto", "observation": "fan deposits", "date_relevance": "post-event", "method": "affine"}],
        "parts": [
            {"part_id": "1a", "seg_id": "1", "part_label": "Catchment", "geometry": "catch", "placement": "approximate", "is_hypo": 0,
             "confidence": "high", "quotes": [{"text": "Das Einzugsgebiet Testbach umfasst eine Fläche von 2,5 km²."}], **common},
            {"part_id": "2a", "seg_id": "2", "part_label": "Fitted fan deposit", "placement": "modelled", "is_hypo": 0, "confidence": "medium",
             "geometry": {"terrain": {"channels": [{"reach": {"chainage": "hm", "from": 0, "to": 6}}], "max_dist_m": 300, "height_m": "$param"}},
             "parameter": {"name": "height above channel", "unit": "m", "fit_area_ha": 3, "bounds": [0, 60], "sensitivity": [5, 20]},
             "quotes": [{"printed_page": 11, "start": "Die Ablagerungen", "end": "Straße auf 50 m unterbrochen wurde."}], **common},
            {"part_id": "2b", "seg_id": "2", "part_label": "Mapped deposit", "placement": "documented", "is_hypo": 0, "confidence": "medium",
             "geometry": {"georef_pixels": {"georef": "img", "pixels": [[10, 10], [90, 10], [90, 90], [10, 90]]}}, "figures": ["3"], "georef": ["img"],
             "quotes": [], **{**common, "evidence_type": "orthophoto"}},
            {"part_id": "3a", "seg_id": "3", "part_label": "Bridge", "placement": "modelled", "is_hypo": 1, "confidence": "low", "rule_5d": True,
             "alternatives": "another crossing", "geometry": {"buffer": {"geom": "bridge", "width": "$param", "cap": "flat"}},
             "parameter": {"name": "deck width", "unit": "m", "value": 6, "sensitivity": [4, 8]},
             "quotes": [{"text": "Der Murgang erreichte die Brücke bei hm 4."}], **common},
        ],
        "checks": [{"label": "catchment", "measure": {"area_km2": "catch"}, "report_value": 2.5}],
        "map": {"context": [{"geom": "bach", "label": "Testbach"}], "chainage_ticks": {"chainage": "hm", "every": 2}},
    }


class SegmentTableTests(TestCase):
    def test_parse_keeps_values(self):
        segs, header = parse_segment_table(SEGMENTS)
        self.assertEqual([s["seg_id"] for s in segs], ["1", "2", "3"])
        self.assertEqual(segs[1]["phase"], "Transportation ")
        self.assertEqual(segs[2]["interact"], "Process-structure")


class ChainageTests(TestCase):
    def test_piecewise_then_translation(self):
        from shapely.geometry import LineString

        ch = Chainage("hm", LineString([(0, 0), (5000, 0)]), 100, [dict(value=0, raw_m=0), dict(value=18.3, raw_m=1783.741)])
        self.assertAlmostEqual(ch.raw(18.3), 1783.741)
        self.assertAlmostEqual(ch.raw(9.15), 1783.741 / 2)
        self.assertAlmostEqual(ch.raw(28), 1783.741 + 970)
        self.assertAlmostEqual(ch.value(ch.raw(7.7)), 7.7)
        self.assertAlmostEqual(ch.anchors[1]["offset_m"], -46.259, places=3)


class GeorefTests(TestCase):
    def test_affine_recovers_transform(self):
        M = np.array([[0.6, -0.05], [0.04, -0.62], [65800, 231000]])
        px = np.array([[10, 10], [900, 30], [40, 800], [850, 870], [400, 400]], float)
        tg = np.c_[px, np.ones(len(px))] @ M
        gcps = [dict(label=str(i), pixel=list(p), target=list(t)) for i, (p, t) in enumerate(zip(px[:4], tg[:4]))]
        gr = Georef("t", {"method": "affine"}, gcps, [dict(label="chk", pixel=list(px[4]), target=list(tg[4]))])
        self.assertLess(gr.rms, 1e-6)
        self.assertLess(gr.checks[0]["error_m"], 1e-6)
        self.assertAlmostEqual(gr.pixel_size, np.sqrt(abs(0.6 * -0.62 - (-0.05 * 0.04))), places=6)

    def test_minimum_gcps_and_methods(self):
        g2 = [dict(pixel=[0, 0], target=[0, 0]), dict(pixel=[10, 0], target=[20, 0])]
        with self.assertRaises(ValueError):
            Georef("t", {"method": "affine"}, list(g2), [])
        sim = Georef("t", {"method": "similarity"}, list(g2), [])
        self.assertIn("Exactly determined", sim.note)
        proj = Georef("p", {"method": "projective"}, [dict(pixel=[0, 0], target=[0, 0]), dict(pixel=[1, 0], target=[2, 0]),
                                                      dict(pixel=[0, 1], target=[0, 2]), dict(pixel=[1, 1], target=[2, 2])], [])
        self.assertTrue(np.allclose(proj.apply(np.array([[0.5, 0.5]])), [[1, 1]]))

    def test_color_rule_safe(self):
        rgb = np.zeros((2, 2, 3), int)
        rgb[0, 0] = [220, 30, 30]
        m = eval_color_rule("(R>175)&(G<110)&(R-G>95)", rgb)
        self.assertTrue(m[0, 0] and not m[1, 1])
        with self.assertRaises(ValueError):
            eval_color_rule("__import__('os')", rgb)


class QuoteTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run = _make_run(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hyphenation_and_pages(self):
        q = resolve_quote(self.run, {"text": "Der Murgang erreichte die Brücke bei hm 4."})
        self.assertTrue(q["verified"])
        self.assertEqual((q["pdf_page"], q["printed_page"]), (1, 10))

    def test_cross_page_with_running_header(self):
        q = resolve_quote(self.run, {"start": "der Bach wurde verfüllt", "end": "auf 50 m unterbrochen wurde."})
        self.assertTrue(q["verified"], q)
        self.assertEqual(q.get("pdf_page_end"), 3)
        self.assertNotIn("Header", q["text"])

    def test_unknown_quote_flagged(self):
        self.assertFalse(resolve_quote(self.run, {"text": "Dieser Satz steht nicht im Bericht."})["verified"])


class BuildDeliverTests(TestCase):
    def setUp(self):
        from PIL import Image

        self.tmp = Path(tempfile.mkdtemp())
        self.run = _make_run(self.tmp)
        Image.new("RGB", (100, 100), "white").save(self.tmp / "tmp/report_inspection/fake.png")
        write_json(self.tmp / "scripts/placements.json", _spec())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stream_oriented_from_mouth(self):
        ev = Evaluator(self.run, _spec())
        self.assertEqual(ev.geom("bach").coords[0], (1000.0, 100.0))
        self.assertAlmostEqual(ev.chainage("hm").anchors[1]["raw_m"], 400.0)

    def test_build_and_deliver(self):
        r = build(Run(self.tmp))
        self.assertEqual(r["errors"], [], r)
        self.assertEqual(r["parts"], 4)
        ev = {e["part_id"]: e for e in __import__("csv").DictReader(open(self.tmp / "outputs/segments_evidence.csv", encoding="utf-8-sig"))}
        fit = json.loads(ev["2a"]["fit_json"])["part"]
        self.assertLessEqual(abs(fit["error_pct"]), 5)
        self.assertEqual(len(json.loads(ev["3a"]["sensitivity_json"])), 3)
        self.assertIn("pdf_page_end", ev["2a"]["quotations_json"])
        geo = json.loads(ev["2b"]["georeferencing_json"])["img"]
        self.assertLess(geo["rms_m"], 1e-6)
        self.assertAlmostEqual(float(ev["2b"]["area_ha"]), 160 * 160 / 1e4, places=3)
        out = deliver(Run(self.tmp), browser_check=False)
        self.assertEqual(out["validation"]["errors"], [], out["validation"])
        with zipfile.ZipFile(out["zip"]) as z:
            self.assertEqual(sorted(z.namelist()), sorted(ZIP_FILES))
        rep = (self.tmp / "outputs/data_report.md").read_text(encoding="utf-8")
        for heading in ("## Data found and used", "## Checks", "## Visual evidence used", "## Sensitivity", "## Uncertain interpretations"):
            self.assertIn(heading, rep)
        html = (self.tmp / "outputs/segments_map.html").read_text(encoding="utf-8")
        from segment_mapping.webmap import embedded_counts

        counts = embedded_counts(html)
        self.assertEqual((counts["parts"], counts["hypotheses"], counts["leaflet_inlined"]), (4, 1, True))
        self.assertNotIn("cdn.jsdelivr", html)  # works offline

    def test_rule_violations_are_errors(self):
        spec = _spec()
        spec["parts"][3]["confidence"] = "high"  # high with is_hypo 1 and rule 5d
        spec["parts"][0]["quotes"] = [{"text": "Nicht im Bericht."}]
        spec["parts"] = spec["parts"][:1] + spec["parts"][3:]  # segment 2 missing
        write_json(self.tmp / "scripts/placements.json", spec)
        errs = "\n".join(build(Run(self.tmp), write=False)["errors"])
        self.assertIn("confidence high requires is_hypo 0", errs)
        self.assertIn("rule_5d parts must be", errs)
        self.assertIn("quotation not found", errs)
        self.assertIn("segment 2 has no part", errs)
        v = validate(Run(self.tmp))
        self.assertTrue(v["errors"])


class FixesFromToolkitRunTests(TestCase):
    def test_similarity_handles_image_y_down(self):
        # map = image scaled by 2, y flipped, shifted
        px = [[0, 0], [100, 0], [0, 100], [100, 100]]
        tg = [[1000 + 2 * x, 5000 - 2 * y] for x, y in px]
        gr = Georef("s", {"method": "similarity"}, [dict(pixel=p, target=t) for p, t in zip(px, tg)], [])
        self.assertLess(gr.rms, 1e-6)

    def test_param_inside_strings(self):
        from segment_mapping.geom import _substitute

        self.assertEqual(_substitute({"rule": "(R>$param)&(G<110)"}, 175), {"rule": "(R>175)&(G<110)"})
        self.assertEqual(_substitute({"width": "$param"}, 7.5), {"width": 7.5})

    def test_unknown_spec_keys_reported(self):
        from segment_mapping.build import spec_key_issues

        issues = spec_key_issues({"parts": [{"part_id": "1a", "geomtery": {}}], "data_gaps": [{"item": "x", "status": "y"}]})
        self.assertTrue(any("geomtery" in i for i in issues))
        self.assertTrue(any("data_gaps" in i and "item" in i for i in issues))


class PromptTemplateTests(TestCase):
    def test_prompt_fills_every_placeholder(self):
        import re
        import subprocess
        import sys

        tmp = Path(tempfile.mkdtemp()).resolve()
        try:
            (tmp / "report.pdf").write_bytes(b"%PDF-1.4\n")
            (tmp / "segments.tsv").write_text(SEGMENTS, encoding="utf-8")
            (tmp / "dem.tif").write_bytes(b"")
            out = tmp / "prompt.txt"
            cmd = [sys.executable, "-m", "segment_mapping", "prompt", "--report", str(tmp / "report.pdf"), "--segments",
                   str(tmp / "segments.tsv"), "--run", str(tmp / "run folder"), "--dem", str(tmp / "dem.tif"), "--out", str(out)]
            subprocess.run(cmd, check=True, capture_output=True, cwd=Path(__file__).parent)
            text = out.read_text(encoding="utf-8")
            left = set(re.findall(r"\[[a-zA-Z][^\]\n]*\]", text)) - {"[options]"}
            self.assertEqual(left, set())
            self.assertIn("2\tTestbach\tDebris flow in channel", text)
            self.assertIn('--dem "', text)
            self.assertNotIn("--catchment \"", text)
            self.assertIn(f'init "{tmp / "run folder"}"', text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
