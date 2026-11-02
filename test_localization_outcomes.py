"""Mixed geometry and unresolved outcomes must survive a full reviewer export."""
import csv
import json
import tempfile
import zipfile
from pathlib import Path
from unittest import TestCase

from segment_mapping.build import build, footprint_warnings
from segment_mapping.deliver import deliver, validate
from segment_mapping.runctx import write_json
from segment_mapping.webmap import embedded_counts
from test_segment_mapping import _make_run, _spec


class LocalizationOutcomeExports(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = _make_run(Path(self.tmp.name))

    def outcomes(self, located=()):
        return [dict(seg_id=s['seg_id'], localization_status='located' if s['seg_id'] in located else 'unresolved',
                     reason='Identified feature' if s['seg_id'] in located else 'Insufficient spatial evidence',
                     event_confidence='high', location_confidence='medium' if s['seg_id'] in located else 'unknown',
                     boundary_confidence='unknown') for s in self.run.segments]

    def save(self, spec):
        write_json(self.run.dir / 'scripts/placements.json', spec)

    def test_geojson_export_repairs_hole_collapsed_by_reprojection(self):
        import geopandas as gpd
        from shapely.geometry import Polygon, box, shape

        hole = [(65629.56480485522, 227318.0768897049),
                (65629.57157442997, 227317.46399825718),
                (65629.57157442998, 227317.46399825718)]
        catchment = Polygon(box(65000, 227000, 66000, 228000).exterior.coords, [hole])
        self.assertTrue(catchment.is_valid)
        self.assertFalse(gpd.GeoSeries([catchment], crs=self.run.crs).to_crs(4326).iloc[0].is_valid)
        self.run.write_layer('catchments', gpd.GeoDataFrame(geometry=[catchment], crs=self.run.crs))
        spec = _spec()
        spec.pop('georef')
        spec.pop('visuals')
        spec['parts'] = [spec['parts'][0]]
        spec['segment_results'] = self.outcomes(('1',))
        self.save(spec)

        self.assertEqual(build(self.run)['errors'], [])
        data = json.loads((self.run.out / 'segments.geojson').read_text(encoding='utf-8'))
        feature = data['features'][0]
        repaired = shape(feature['geometry'])
        self.assertTrue(repaired.is_valid)
        self.assertFalse(repaired.is_empty)
        self.assertEqual(repaired.geom_type, 'Polygon')
        self.assertEqual(feature['properties']['part_id'], '1a')
        projected = gpd.GeoSeries([repaired], crs=4326).to_crs(self.run.crs).iloc[0]
        self.assertAlmostEqual(projected.area, catchment.area, places=2)
        native = gpd.read_file(self.run.out / 'segments.shp').geometry.iloc[0]
        self.assertTrue(native.equals(catchment))
        self.assertEqual(validate(self.run)['errors'], [])

    def test_all_unresolved_delivers_empty_geometry_and_complete_ledger(self):
        self.save(dict(parts=[], segment_results=self.outcomes()))
        result = build(self.run)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['parts'], 0)
        package = deliver(self.run, browser_check=False)
        self.assertEqual(package['validation']['errors'], [])
        self.assertFalse((self.run.out / 'segments.shp').exists())
        data = json.loads((self.run.out / 'segments.geojson').read_text())
        self.assertEqual(data['features'], [])
        with (self.run.out / 'segment_results.csv').open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r['localization_status'] == 'unresolved' for r in rows))
        counts = embedded_counts((self.run.out / 'segments_map.html').read_text(encoding='utf-8'))
        self.assertEqual(counts['parts'], 0)
        with zipfile.ZipFile(package['zip']) as z:
            self.assertIn('segment_results.json', z.namelist())
            self.assertIn('segments.geojson', z.namelist())

    def test_native_point_line_and_polygon_keep_dimensions_and_roles(self):
        spec = _spec()
        spec.pop('georef')
        spec.pop('visuals')
        spec['parts'] = [spec['parts'][0], {**spec['parts'][0], 'part_id': '2p', 'seg_id': '2',
                         'geometry': {'centroid': 'bridge'}, 'geometry_role': 'location'},
                         {**spec['parts'][0], 'part_id': '3l', 'seg_id': '3', 'geometry': 'bridge',
                          'geometry_role': 'location'}]
        spec['segment_results'] = self.outcomes(('1', '2', '3'))
        self.save(spec)
        result = build(self.run)
        self.assertEqual(result['errors'], [])
        package = deliver(self.run, browser_check=False)
        self.assertEqual(package['validation']['errors'], [])
        data = json.loads((self.run.out / 'segments.geojson').read_text(encoding='utf-8'))
        self.assertEqual({f['geometry']['type'] for f in data['features']}, {'Polygon', 'Point', 'LineString'})
        for f in data['features']:
            if f['geometry']['type'] != 'Polygon':
                self.assertIsNone(f['properties']['area_ha'])
                self.assertEqual(f['properties']['geometry_role'], 'location')
                self.assertEqual(f['properties']['boundary_confidence'], 'unknown')
        import geopandas as gpd
        self.assertEqual(len(gpd.read_file(self.run.out / 'segments.shp')), 1)

    def test_status_and_role_are_validated(self):
        spec = _spec()
        spec.pop('georef')
        spec.pop('visuals')
        spec['parts'] = [{**spec['parts'][0], 'geometry_role': 'invented'}]
        spec['segment_results'] = self.outcomes()
        self.save(spec)
        errors = '\n'.join(build(self.run, write=False)['errors'])
        self.assertIn('geometry_role must be', errors)
        self.assertIn('unresolved outcome must have no parts', errors)

    def test_tampered_outcomes_fail_delivery_validation(self):
        self.save(dict(parts=[], segment_results=self.outcomes()))
        build(self.run)
        write_json(self.run.out / 'segment_results.json', [])
        self.assertIn('segment_results.json differs', '\n'.join(validate(self.run)['errors']))

    def test_footprint_reuse_warns_for_events_but_not_broad_triggers(self):
        from shapely.geometry import box
        a = dict(seg_id='1', part_id='a', process='Heavy rain', geometry=box(0, 0, 10, 10))
        b = dict(seg_id='2', part_id='b', process='Channel erosion', geometry=a['geometry'])
        self.assertEqual(footprint_warnings([a, b]), [])
        a['process'] = 'Rockfall'
        self.assertTrue(footprint_warnings([a, b]))

    def test_direct_specs_enforce_geometry_and_boundary_semantics(self):
        for geometry, role, boundary, expected in [
            ({'centroid': 'bridge'}, 'event_extent', 'unknown', 'event_extent requires a polygon'),
            ('bridge', 'location', 'high', 'requires unknown boundary_confidence'),
            ('catch', 'event_extent', 'high', 'requires a documented outline'),
        ]:
            with self.subTest(role=role, boundary=boundary):
                spec = _spec()
                spec.pop('georef')
                spec.pop('visuals')
                spec['parts'] = [{**spec['parts'][0], 'geometry': geometry, 'geometry_role': role,
                                  'boundary_confidence': boundary}]
                spec['segment_results'] = self.outcomes(('1',))
                self.save(spec)
                self.assertIn(expected, '\n'.join(build(self.run, write=False)['errors']))

    def test_direct_outcomes_enforce_location_and_boundary_support(self):
        outcomes = self.outcomes()
        outcomes[0]['location_confidence'] = 'high'
        outcomes[1]['boundary_confidence'] = 'medium'
        self.save(dict(parts=[], segment_results=outcomes))
        errors = '\n'.join(build(self.run, write=False)['errors'])
        self.assertIn('segment 1: unresolved requires unknown', errors)
        self.assertIn('segment 2: unresolved requires unknown', errors)

    def test_delivery_rechecks_semantics_after_export_tampering(self):
        spec = _spec()
        spec.pop('georef')
        spec.pop('visuals')
        spec['parts'] = [{**spec['parts'][0], 'geometry': {'centroid': 'bridge'}, 'geometry_role': 'location'}]
        spec['segment_results'] = self.outcomes(('1',))
        self.save(spec)
        self.assertEqual(build(self.run)['errors'], [])
        path = self.run.out / 'segments.geojson'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['features'][0]['properties'].update(geometry_role='event_extent', boundary_confidence='high')
        write_json(path, data)
        errors = '\n'.join(validate(self.run)['errors'])
        self.assertIn('event_extent requires a polygon', errors)
        self.assertIn('requires a documented outline', errors)
        self.assertIn('differs between canonical geometry and evidence CSV', errors)

    def test_map_escapes_feature_and_control_text(self):
        from segment_mapping.webmap import build_map_data
        from shapely.geometry import Point
        attack = '<img src=x onerror=alert(1)>'
        self.run.config['segments'][0]['process'] = attack
        self.run.config['basemaps'] = [{'name': attack, 'url': 'OpenStreetMap'}]
        self.run.save_config()
        spec = _spec()
        spec.pop('georef')
        spec.pop('visuals')
        spec['parts'] = [{**spec['parts'][0], 'part_label': attack}]
        spec['segment_results'] = self.outcomes(('1',))
        self.save(spec)
        self.assertEqual(build(self.run)['errors'], [])
        data = build_map_data(self.run, lambda row: '<p>escaped popup</p>', 'Title',
                              context=[(attack, Point(1000, 1000), '#444')], ticks=[(attack, Point(1000, 1000))])
        for text in [data['segments'][0]['name'], data['segments'][0]['features']['features'][0]['properties']['tooltip'],
                     data['tiles'][0]['name'], data['context'][0]['label'], data['ticks'][0]['label']]:
            self.assertNotIn('<img', text)
            self.assertIn('&lt;img', text)
