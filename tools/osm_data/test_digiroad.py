"""Digiroad's four layers: fetched page by page from a fake WFS into files of the features as served, built into
zoom-11 tiles, and uploaded in cells beside those files."""

from contextlib import ExitStack
from datetime import datetime, timezone
import gzip
import io
import json
import lzma
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from pmtiles.reader import MmapSource, all_tiles

from tools.osm_data import digiroad, publish
from tools.osm_data.test_publish import ENV
from tools.osm_data.test_routing import SITE, RoutingHost

T0 = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)
BASE = SITE + '/polku-digiroad'
FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def road(n, properties=None, kind='LineString', layer='dr_liikennemaara'):
    """A short road near Tampere, a busy one by default; n=0 crosses the edge of two zoom-11 tiles."""
    line = [[23.73 + 0.0001 * n, 61.49, 101.5], [23.73 + 0.0001 * n + 0.004, 61.491, 102.25]]
    if n == 0:
        line = [[23.906, 61.5], [23.910, 61.5]]  # 23.90625° is a zoom-11 tile's west edge
    geometry = {'type': kind, 'coordinates': [line] if kind == 'MultiLineString' else line}
    properties = properties or {'id': str(100 + n), 'arvo': 1000, 'muokkauspv': f'{n + 1:02}.07.2022 12:00:00'}
    return {'type': 'Feature', 'id': f'{layer}.{100 + n}', 'geometry': geometry, 'properties': properties}


def layers():
    """A few features of each layer, as the WFS serves them; a private road's ID is its link's."""
    paved = [road(n, {'id': str(100 + n), 'arvo': 99, 'hallinn_lk': 1 + n, 'muokkauspv': '25.10.2024 10:54:16'},
                  layer='dr_paallystetty_tie') for n in range(4)]
    private = [road(n, {'link_id': f'{n}a-b:1', 'tienimi_su': 'Mäntytie', 'lisatiedot': 'ei toimitettu'},
                    layer='tiekunnalliset_yksityistiet') for n in range(2)]
    for feature in private:
        feature['id'] = 'tiekunnalliset_yksityistiet.' + feature['properties']['link_id']
    speed = [road(n, {'id': '7', 'arvo': 60, 'muokkauspv': '15.10.2015 16:00:45', 'id_pk': 100 + n},
                  layer='dr_nopeusrajoitus') for n in range(5)]
    busy = [road(n) for n in range(7)] + [road(7, {'id': '107', 'arvo': 78500, 'muokkauspv': '08.07.2022 12:00:00'},
                                              'MultiLineString')]
    return {'digiroad:dr_liikennemaara': busy, 'digiroad:dr_paallystetty_tie': paved,
            'digiroad:tiekunnalliset_yksityistiet': private, 'digiroad:dr_nopeusrajoitus': speed}


def served(path):
    """The features in a fetched file."""
    with lzma.open(path) as lines:
        return [json.loads(line) for line in lines]


class WFS:
    """Pages of each layer's features, as GeoServer serves them; failing counts the requests still to fail."""
    def __init__(self, features, matched=None):
        self.features, self.matched, self.failing, self.requests = features, matched, 0, []

    def __call__(self, request, timeout=None):
        query = {k: v[0] for k, v in parse_qs(urlsplit(request.full_url).query).items()}
        self.requests.append(query)
        if request.get_header('User-agent') != digiroad.AGENT:
            raise AssertionError('an honest User-Agent')
        if self.failing:
            self.failing -= 1
            raise URLError('connection reset')
        start, count, features = int(query['startIndex']), int(query['count']), self.features[query['typeNames']]
        body = {'type': 'FeatureCollection', 'numberMatched': self.matched or len(features),
                'timeStamp': '2026-11-23T17:27:05.820Z', 'features': features[start:start + count]}
        return io.BytesIO(json.dumps(body).encode())


class DigiroadTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(digiroad, 'PAGE', 3))
        self.wfs = WFS(layers())
        patches.enter_context(patch.object(digiroad.urllib.request, 'urlopen', self.wfs))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def fetch(self, directory):
        directory.mkdir()
        for name in digiroad.DATASETS:
            digiroad.fetch(name, directory, self.sleeps.append)
        return directory

    def test_the_fetch_asks_page_by_page_and_keeps_every_feature_as_served(self):
        digiroad.fetch('traffic', self.tmp, self.sleeps.append)
        self.assertEqual([q['startIndex'] for q in self.wfs.requests], ['0', '3', '6'])
        self.assertEqual(self.sleeps, [digiroad.PAUSE] * 2, 'one page at a time, with a pause between')
        query = self.wfs.requests[0]
        self.assertEqual((query['typeNames'], query['cql_filter'], query['sortBy'], query['srsName']),
                         ('digiroad:dr_liikennemaara', 'arvo>=800', 'id', 'EPSG:4326'))
        self.assertNotIn('propertyName', query, 'every property')
        self.assertEqual(served(self.tmp / 'traffic.jsonl.xz'), self.wfs.features['digiroad:dr_liikennemaara'])
        for name, order in [('paved', 'id'), ('private', 'link_id'), ('speed', 'id_pk')]:
            self.wfs.requests.clear()
            digiroad.fetch(name, self.tmp, self.sleeps.append)
            layer = digiroad.DATASETS[name][0]
            self.assertEqual({(q['typeNames'], q['sortBy'], 'cql_filter' in q) for q in self.wfs.requests}, {(layer, order, False)})
            self.assertEqual(served(self.tmp / f'{name}.jsonl.xz'), self.wfs.features[layer], 'all of them, each once')
        again = self.tmp / 'again'
        self.fetch(again)
        for name in digiroad.DATASETS:
            self.assertEqual((again / f'{name}.jsonl.xz').read_bytes(), (self.tmp / f'{name}.jsonl.xz').read_bytes(),
                             'the same features give the same bytes')

    def test_missing_repeated_or_unexpected_features_stop_the_copy(self):
        self.wfs.matched = 9
        with self.assertRaisesRegex(ValueError, 'gave 8 of 9 traffic features'):
            digiroad.fetch('traffic', self.tmp, self.sleeps.append)
        self.wfs.matched = None
        speed = self.wfs.features['digiroad:dr_nopeusrajoitus']
        speed[3] = speed[2]  # a page that slipped: one feature twice, another not at all
        with self.assertRaisesRegex(ValueError, 'repeated dr_nopeusrajoitus.102'):
            digiroad.fetch('speed', self.tmp, self.sleeps.append)
        self.wfs.features = layers()
        for layer, n, properties in [('digiroad:dr_liikennemaara', 2, {'arvo': 500}),
                                     ('digiroad:dr_paallystetty_tie', 1, {'hallinn_lk': 0}),
                                     ('digiroad:tiekunnalliset_yksityistiet', 0, {'lisatiedot': None}),
                                     ('digiroad:dr_nopeusrajoitus', 4, {'arvo': 110})]:
            with self.subTest(layer):
                self.wfs.features = layers()
                feature = self.wfs.features[layer][n]
                feature['properties'] |= properties
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaisesRegex(ValueError, 'Unexpected Digiroad feature ' + feature['id']):
                        digiroad.build(self.fetch(Path(tmp) / 'release'), T0)
        self.wfs.features = layers()
        for features in self.wfs.features.values():
            for feature in features:
                feature['properties'].pop('muokkauspv', None)
        with self.assertRaisesRegex(ValueError, 'no edit times'):
            digiroad.build(self.fetch(self.tmp / 'release'), T0)

    def test_a_failed_page_waits_once_then_stops(self):
        self.wfs.failing = 1
        digiroad.fetch('traffic', self.tmp, self.sleeps.append)
        self.assertEqual(len(served(self.tmp / 'traffic.jsonl.xz')), 8)
        self.assertIn(digiroad.BACK_OFF, self.sleeps)
        self.wfs.failing = 2
        with self.assertRaisesRegex(RuntimeError, 'traffic failed twice at feature 0'):
            digiroad.fetch('traffic', self.tmp, self.sleeps.append)

    def tiles(self, path):
        with open(path, 'rb') as f:
            return {xyz: json.loads(gzip.decompress(data)) for xyz, data in all_tiles(MmapSource(f))}

    def test_the_release_has_each_feature_in_every_tile_it_touches_beside_the_features_as_served(self):
        manifest = digiroad.build(self.fetch(self.tmp / 'release'), T0)
        tiles = self.tiles(self.tmp / 'release' / 'traffic.pmtiles')
        self.assertEqual({z for z, _, _ in tiles}, {11})
        self.assertEqual(sorted(xyz for xyz, t in tiles.items() if any(e['id'] == 100 for e in t['elements'])),
                         [(11, 1159, 577), (11, 1160, 577)])
        self.assertTrue(all(t['schema'] == 1 for t in tiles.values()))
        elements = {name: [e for t in self.tiles(self.tmp / 'release' / f'{name}.pmtiles').values() for e in t['elements']]
                    for name in digiroad.DATASETS}
        lines = [[[23.7301, 61.49], [23.7341, 61.491]]]  # six decimals, no heights
        self.assertIn({'id': 101, 'aadt': 1000, 'lines': lines}, elements['traffic'])
        self.assertIn({'id': 101, 'arvo': 99, 'hallinn_lk': 2, 'lines': lines}, elements['paved'])
        self.assertIn({'id': '1a-b:1', 'lisatiedot': 'ei toimitettu', 'lines': lines}, elements['private'])
        self.assertIn({'id': 101, 'arvo': 60, 'lines': lines}, elements['speed'], "the feature's own ID, not Digiroad's id")
        self.assertEqual(sorted(manifest['datasets']), ['paved', 'private', 'speed', 'traffic'])
        files = {f['path']: f for f in manifest['files']}
        for name, dataset in manifest['datasets'].items():
            self.assertEqual((dataset['format'], dataset['zoom'], dataset['features'], dataset['url'], dataset['raw']),
                             ('pmtiles-json', 11, len(self.wfs.features[digiroad.DATASETS[name][0]]),
                              f'{name}.pmtiles', f'{name}.jsonl.xz'))
            self.assertEqual(files[dataset['raw']]['bytes'], (self.tmp / 'release' / dataset['raw']).stat().st_size)
        self.assertEqual(manifest['datasets']['traffic']['source']['filter'], 'arvo>=800')
        self.assertEqual(manifest['source']['timestamp'], '2024-10-25T07:54:16Z', "the newest edit of all, in Finland's summer time")
        self.assertTrue(manifest['release'].startswith('20261123T180000Z-'))
        self.assertIn('Väylävirasto', manifest['attribution'])
        self.assertIn('creativecommons.org/licenses/by/4.0', (self.tmp / 'release' / 'LICENSE.txt').read_text())
        publish.local_release(self.tmp / 'release')
        again = digiroad.build(self.fetch(self.tmp / 'again'), T0)
        self.assertEqual(again['files'], manifest['files'], 'the same features give the same bytes')

    def test_the_fixtures_are_what_the_wfs_answers_around_tampere_give(self):
        self.wfs.features = {layer: served(FIXTURES / f'digiroad-{name}.jsonl.xz')
                             for name, (layer, *_) in digiroad.DATASETS.items()}
        with patch.object(digiroad, 'PAGE', 5000):
            manifest = digiroad.build(self.fetch(self.tmp / 'release'), T0)
        for name in digiroad.DATASETS:
            with self.subTest(name):
                self.assertEqual(lzma.decompress((self.tmp / 'release' / f'{name}.jsonl.xz').read_bytes()),
                                 lzma.decompress((FIXTURES / f'digiroad-{name}.jsonl.xz').read_bytes()))
                self.assertEqual((self.tmp / 'release' / f'{name}.pmtiles').read_bytes(), (FIXTURES / f'digiroad-{name}.pmtiles').read_bytes())
        self.assertEqual({name: d['features'] for name, d in manifest['datasets'].items()},
                         {'traffic': 89, 'paved': 2638, 'private': 5, 'speed': 2132})

    def test_the_release_goes_up_in_cells_beside_polku_osm_with_the_features_as_served(self):
        self.fetch(self.tmp / 'release')
        shutil.copytree(self.tmp / 'release', self.tmp / 'again')
        host = RoutingHost()
        host.dirs.add(BASE)
        host.files[BASE + '/.htaccess'] = b'installed'
        with patch.dict('os.environ', ENV | {'OSM_CELLS': 'true'}, clear=True), \
                patch.object(publish, 'open_sftp', host.ftp), patch.object(publish.urllib.request, 'urlopen', host.http):
            digiroad.build(self.tmp / 'release', T0)
            with patch.object(publish.maintenance, 'usage', return_value=digiroad.BUDGET - 4096):
                with self.assertRaisesRegex(RuntimeError, r'polku-digiroad storage budget exceeded \(4 GiB\)'):
                    publish.upload(self.tmp / 'release', folder=digiroad.FOLDER, budget=digiroad.BUDGET)
            self.assertEqual(host.uploads, [], 'a full folder takes nothing')
            publish.upload(self.tmp / 'release', folder=digiroad.FOLDER, budget=digiroad.BUDGET)
            raw = host.files[BASE + '/candidates/current.json']
            manifest = json.loads(raw)
            self.assertEqual(host.files[f"{BASE}/releases/{manifest['release']}/manifest.json"], raw)
            files = {f['path']: f for f in manifest['files']}
            for name, dataset in manifest['datasets'].items():
                self.assertEqual(dataset['format'], publish.CELLS)
                index = host.files[f"{BASE}/{publish.place(manifest['release'], publish.index_entry(dataset) | {'url': dataset['url']})}"]
                for cell in publish.cells_of(index, publish.index_entry(dataset)):
                    self.assertIn(f"{BASE}/files/{cell['sha256']}.pmtiles", host.files)
                self.assertEqual(host.files[f"{BASE}/{publish.place(manifest['release'], files[dataset['raw']])}"],
                                 (self.tmp / 'release' / dataset['raw']).read_bytes(), 'the features as served, shared')
            self.assertFalse(any(p.startswith(SITE + '/polku-osm/') for p in host.files), 'nothing in polku-osm')
            host.files[BASE + '/current.json'] = json.dumps(publish.current(manifest)).encode()  # as a promotion
            host.uploads.clear()
            digiroad.build(self.tmp / 'again', datetime(2026, 11, 24, tzinfo=timezone.utc))
            with patch.object(publish.maintenance, 'usage', return_value=digiroad.BUDGET - 4096):
                with self.assertRaisesRegex(RuntimeError, r'polku-digiroad storage budget exceeded \(4 GiB\)'):
                    publish.upload(self.tmp / 'again', folder=digiroad.FOLDER, budget=digiroad.BUDGET)
            self.assertEqual(host.uploads, [], 'with a live release too')
            publish.upload(self.tmp / 'again', folder=digiroad.FOLDER, budget=digiroad.BUDGET)
            self.assertEqual([p for p, _, _ in host.uploads if p.endswith(('.pmtiles', '.xz'))], [],
                             'the same features send no cell or file again')
            del host.files[BASE + '/.htaccess']
            with self.assertRaisesRegex(RuntimeError, 'run install-hosting first'):
                publish.upload(self.tmp / 'again', folder=digiroad.FOLDER, budget=digiroad.BUDGET)

    def test_the_budget_holds_three_copies_of_the_release(self):
        # measured 2026-11-23 on sample pages: about 0.4 GB of tiles and 0.56 GB of the features as served
        self.assertGreaterEqual(digiroad.BUDGET, 3 * 0.96e9 * 1.3)


if __name__ == '__main__':
    unittest.main()
