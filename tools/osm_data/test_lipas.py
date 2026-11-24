"""LIPAS's routes and places: fetched page by page from a fake WFS into files of the features as served, built into
zoom-9 tiles of each type, and uploaded in cells beside those files."""

from contextlib import ExitStack
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pmtiles.reader import MmapSource, all_tiles

from tools.osm_data import cells, lipas, publish, wfs
from tools.osm_data.test_digiroad import WFS, served
from tools.osm_data.test_publish import ENV
from tools.osm_data.test_routing import SITE, RoutingHost

T0 = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
BASE = SITE + '/polku-lipas'
FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
FIXED = ('4411', '301')  # the fixtures' types: Multamäki's MTB routes and the lean-tos around them
TYPES = dict(lipas.DATASETS)  # Polku's types; the tests copy the fixtures' two, and 4412 with none


def part(n, reitti=1, x=443341.2822, y=6923064.2259, edited='2026-01-13T08:59:27.835Z'):
    """A part of an MTB route near Jyväskylä, as the WFS serves it; n=0 crosses the edge of two zoom-9 tiles."""
    line = [[x, y], [x + 400, y + 30.5]] if n else [[449200.0, 6930000.0], [449400.0, 6930000.0]]  # 26.015625°: an edge
    return {'type': 'Feature', 'id': f'lipas_4411_maastopyorailyreitti.{n}-{reitti}',
            'geometry': {'type': 'LineString', 'coordinates': line},
            'properties': {'id': 600 + n, 'reitti_id': reitti, 'nimi_fi': f'Lenkki {n}', 'nimi_se': None,
                           'tyyppikoodi': 4411, 'kunta_nimi_fi': 'Jyväskylä', 'reitin_pituus_km': '18',
                           'puhelinnumero': '014 266 0000', 'muokattu_viimeksi': edited, 'pintamateriaali': ''}}


def shelter(n):
    """A lean-to near the route, as the WFS serves it."""
    return {'type': 'Feature', 'id': f'lipas_301_laavu_kota_kammi.{n}',
            'geometry': {'type': 'Point', 'coordinates': [443500.5 + n, 6923100.25]},
            'properties': {'id': 700 + n, 'nimi_fi': 'Laavu', 'tyyppikoodi': 301, 'yleiso_wc': 'true',
                           'muokattu_viimeksi': '2025-06-30T12:00:00Z'}}


def layers():
    """Two routes of three parts in all, three lean-tos, and nothing in the other types' layers."""
    found = {TYPES['4412'][0]: []}
    found['lipas:lipas_4411_maastopyorailyreitti'] = [part(0), part(1, 1), part(1, 2, x=444000.0)]
    found['lipas:lipas_301_laavu_kota_kammi'] = [shelter(n) for n in range(3)]
    return found


class LipasTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(lipas, 'PAGE', 2))
        patches.enter_context(patch.object(lipas, 'DATASETS', {t: TYPES[t] for t in ('301', '4411')}))
        self.wfs = WFS(layers())
        patches.enter_context(patch.object(wfs.urllib.request, 'urlopen', self.wfs))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def fetch(self, directory):
        directory.mkdir()
        for name in lipas.DATASETS:
            wfs.fetch(lipas, name, directory, self.sleeps.append)
        return directory

    def tiles(self, path):
        with open(path, 'rb') as f:
            return {xyz: json.loads(gzip.decompress(data)) for xyz, data in all_tiles(MmapSource(f))}

    def test_the_fetch_asks_each_type_in_finlands_grid_ordered_by_place_and_part(self):
        wfs.fetch(lipas, '4411', self.tmp, self.sleeps.append)
        query = self.wfs.requests[0]
        self.assertEqual((query['typeNames'], query['srsName'], query['sortBy']),
                         ('lipas:lipas_4411_maastopyorailyreitti', 'EPSG:3067', 'id,reitti_id'))
        self.assertEqual([q['startIndex'] for q in self.wfs.requests], ['0', '2'])
        self.assertEqual(self.sleeps, [lipas.PAUSE])
        self.assertEqual(served(self.tmp / '4411.jsonl.xz'), self.wfs.features['lipas:lipas_4411_maastopyorailyreitti'])
        wfs.fetch(lipas, '301', self.tmp, self.sleeps.append)
        self.assertEqual(self.wfs.requests[-1]['sortBy'], 'id', 'a place has no parts')
        with patch.object(lipas, 'DATASETS', TYPES), self.assertRaisesRegex(ValueError, 'gave 0 of 0 4412 features'):
            wfs.fetch(lipas, '4412', self.tmp, self.sleeps.append)  # a type with none stops the copy, as Digiroad's
        self.assertEqual(list(TYPES)[:3], ['202', '203', '204'])
        self.assertEqual(len(TYPES), 25, "Polku's types but landing places")

    def test_a_feature_of_another_kind_or_twice_stops_the_fetch_on_its_page(self):
        for n, change in [(1, {'tyyppikoodi': 4412}), (2, {'reitti_id': None}), (0, {'id': '600'})]:
            with self.subTest(change):
                self.wfs.features, self.wfs.requests = layers(), []
                feature = self.wfs.features['lipas:lipas_4411_maastopyorailyreitti'][n]
                feature['properties'] |= change
                with self.assertRaisesRegex(ValueError, 'Unexpected LIPAS feature ' + feature['id']):
                    wfs.fetch(lipas, '4411', self.tmp, self.sleeps.append)
                self.assertEqual(len(self.wfs.requests), n // lipas.PAGE + 1)
        self.wfs.features = layers()
        self.wfs.features['lipas:lipas_301_laavu_kota_kammi'][0]['geometry'] = {'type': 'LineString', 'coordinates': [[1, 2], [3, 4]]}
        with self.assertRaisesRegex(ValueError, 'Unexpected LIPAS feature lipas_301_laavu_kota_kammi.0'):
            wfs.fetch(lipas, '301', self.tmp, self.sleeps.append)
        self.wfs.features = layers()
        shelters = self.wfs.features['lipas:lipas_301_laavu_kota_kammi']
        shelters[2] = shelters[1]  # a page that slipped: one row twice, another not at all
        with self.assertRaisesRegex(ValueError, 'repeated lipas_301_laavu_kota_kammi.1'):
            wfs.fetch(lipas, '301', self.tmp, self.sleeps.append)
        self.wfs.features = layers()
        twin = dict(shelters[0], id='lipas_301_laavu_kota_kammi.twin')  # LIPAS's own duplicate row: kept, as served
        self.wfs.features['lipas:lipas_301_laavu_kota_kammi'].append(twin)
        wfs.fetch(lipas, '301', self.tmp, self.sleeps.append)
        self.assertEqual(len(served(self.tmp / '301.jsonl.xz')), 4)

    def test_each_type_is_its_own_archive_with_what_polku_reads_and_the_geometry_as_served(self):
        manifest = wfs.build(lipas, self.fetch(self.tmp / 'release'), T0)
        self.assertEqual(sorted(manifest['datasets']), ['301', '4411'])
        tiles = self.tiles(self.tmp / 'release' / '4411.pmtiles')
        self.assertEqual({z for z, _, _ in tiles}, {9})
        self.assertEqual(sorted(xyz for xyz, t in tiles.items() if any(e['properties']['id'] == 600 for e in t['elements'])),
                         [(9, 292, 141), (9, 293, 141)], 'whole, in every tile it touches')
        elements = [e for t in tiles.values() for e in t['elements']]
        self.assertIn({'properties': {'id': 601, 'reitti_id': 2, 'nimi_fi': 'Lenkki 1', 'kunta_nimi_fi': 'Jyväskylä',
                                      'reitin_pituus_km': '18', 'pintamateriaali': ''},
                       'geometry': {'type': 'LineString', 'coordinates': [[444000.0, 6923064.2259], [444400.0, 6923094.7259]]}},
                      elements, 'no phone number, edit time or empty field, and the coordinates as served')
        self.assertEqual([e['properties']['reitti_id'] for t in tiles.values() for e in t['elements'] if e['properties']['id'] == 601],
                         [1, 2], 'by place and part')
        shelters = [e for t in self.tiles(self.tmp / 'release' / '301.pmtiles').values() for e in t['elements']]
        self.assertEqual(shelters[0], {'properties': {'id': 700, 'nimi_fi': 'Laavu', 'yleiso_wc': 'true'},
                                       'geometry': {'type': 'Point', 'coordinates': [443500.5, 6923100.25]}})
        route = manifest['datasets']['4411']
        self.assertEqual((route['format'], route['zoom'], route['features'], route['url'], route['raw']),
                         ('pmtiles-json', 9, 3, '4411.pmtiles', '4411.jsonl.xz'))
        self.assertEqual(route['source'], {'url': lipas.SOURCE, 'layer': 'lipas:lipas_4411_maastopyorailyreitti',
                                           'timestamp': '2026-01-13T06:59:27Z'}, "LIPAS's Z is Finland's time")
        self.assertEqual(manifest['source']['timestamp'], '2026-01-13T06:59:27Z')
        self.assertIn('University of Jyväskylä', manifest['attribution'])
        self.assertIn('creativecommons.org/licenses/by/4.0', (self.tmp / 'release' / 'LICENSE.txt').read_text())
        publish.local_release(self.tmp / 'release')
        self.assertEqual(lipas.lon_lat(408178.2479, 6686001.2778), (25.338598000107503, 60.300201994603974),
                         "LIPAS's API's longitude and latitude for the same point")

    def test_the_fixtures_are_what_the_wfs_answers_around_multamaki_give(self):
        self.wfs.features = {TYPES[name][0]: served(FIXTURES / f'lipas-{name}.jsonl.xz') for name in FIXED}
        with patch.object(lipas, 'PAGE', 5000):
            manifest = wfs.build(lipas, self.fetch(self.tmp / 'release'), T0)
        for name in FIXED:
            with self.subTest(name):
                self.assertEqual((self.tmp / 'release' / f'{name}.pmtiles').read_bytes(),
                                 (FIXTURES / f'lipas-{name}.pmtiles').read_bytes())
                self.assertEqual(cells.encoded(cells.index(self.tmp / 'release' / f'{name}.pmtiles', 4096)),
                                 (FIXTURES / f'lipas-{name}-cells' / 'index.json').read_bytes(), 'and their cells, as Polku reads them')
        self.assertEqual({name: d['features'] for name, d in manifest['datasets'].items()}, {'4411': 3, '301': 6})

    def test_the_release_goes_up_in_cells_into_polku_lipas_and_the_same_features_send_nothing_again(self):
        self.fetch(self.tmp / 'release')
        host = RoutingHost()
        host.dirs.add(BASE)
        host.files[BASE + '/.htaccess'] = b'installed'
        with patch.dict('os.environ', ENV | {'OSM_CELLS': 'true'}, clear=True), \
                patch.object(publish, 'open_sftp', host.ftp), patch.object(publish.urllib.request, 'urlopen', host.http):
            wfs.build(lipas, self.tmp / 'release', T0)
            publish.upload(self.tmp / 'release', folder=lipas.FOLDER, budget=lipas.BUDGET)
            manifest = json.loads(host.files[BASE + '/candidates/current.json'])
            self.assertEqual({d['format'] for d in manifest['datasets'].values()}, {publish.CELLS})
            self.assertFalse(any(p.startswith(SITE + '/polku-osm/') for p in host.files), 'nothing in polku-osm')
            host.files[BASE + '/current.json'] = json.dumps(publish.current(manifest)).encode()  # as a promotion
            host.uploads.clear()
            wfs.build(lipas, self.tmp / 'release', datetime(2026, 12, 1, tzinfo=timezone.utc))
            publish.upload(self.tmp / 'release', folder=lipas.FOLDER, budget=lipas.BUDGET)
            self.assertEqual([p for p, _, _ in host.uploads if p.endswith(('.pmtiles', '.xz'))], [],
                             'a week with no changes sends no cell or file again')


if __name__ == '__main__':
    unittest.main()
