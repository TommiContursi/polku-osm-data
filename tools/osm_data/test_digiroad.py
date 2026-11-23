"""Digiroad's busy roads: fetched page by page from a fake WFS, built into zoom-11 tiles, and uploaded in cells."""

from contextlib import ExitStack
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
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


def road(n, aadt=1000, kind='LineString'):
    """A short road near Tampere; n=0 crosses the edge of two zoom-11 tiles."""
    line = [[23.73 + 0.0001 * n, 61.49, 101.5], [23.73 + 0.0001 * n + 0.004, 61.491, 102.25]]
    if n == 0:
        line = [[23.906, 61.5], [23.910, 61.5]]  # 23.90625° is a zoom-11 tile's west edge
    geometry = {'type': kind, 'coordinates': [line] if kind == 'MultiLineString' else line}
    return {'type': 'Feature', 'id': f'dr_liikennemaara.{100 + n}', 'geometry': geometry,
            'properties': {'id': str(100 + n), 'arvo': aadt, 'muokkauspv': f'{n + 1:02}.07.2022 12:00:00'}}


class WFS:
    """Pages of the features, as GeoServer serves them; failing counts the requests still to fail."""
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
        start, count = int(query['startIndex']), int(query['count'])
        body = {'type': 'FeatureCollection', 'numberMatched': self.matched or len(self.features),
                'timeStamp': '2026-11-23T17:27:05.820Z', 'features': self.features[start:start + count]}
        return io.BytesIO(json.dumps(body).encode())


class DigiroadTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(digiroad, 'PAGE', 3))
        self.wfs = WFS([road(n) for n in range(7)] + [road(7, 78500, 'MultiLineString')])
        patches.enter_context(patch.object(digiroad.urllib.request, 'urlopen', self.wfs))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_the_fetch_asks_page_by_page_and_keeps_the_busy_roads(self):
        roads, edited = digiroad.fetch(self.sleeps.append)
        self.assertEqual([q['startIndex'] for q in self.wfs.requests], ['0', '3', '6'])
        self.assertEqual(self.sleeps, [digiroad.PAUSE] * 2, 'one page at a time, with a pause between')
        query = self.wfs.requests[0]
        self.assertEqual((query['typeNames'], query['cql_filter'], query['sortBy'], query['srsName']),
                         ('digiroad:dr_liikennemaara', 'arvo>=800', 'id', 'EPSG:4326'))
        self.assertEqual(sorted(roads), list(range(100, 108)))
        self.assertEqual(roads[101], {'id': 101, 'aadt': 1000, 'lines': [[[23.7301, 61.49], [23.7341, 61.491]]]},
                         'six decimals, no heights')
        self.assertEqual((roads[107]['aadt'], len(roads[107]['lines'])), (78500, 1))
        self.assertEqual(edited, '2022-07-08T09:00:00Z', "the newest edit, in Finland's summer time")

    def test_missing_or_unexpected_roads_stop_the_fetch(self):
        self.wfs.matched = 9
        with self.assertRaisesRegex(ValueError, 'gave 8 of 9 busy roads'):
            digiroad.fetch(self.sleeps.append)
        self.wfs.matched = None
        self.wfs.features[2] = road(2, aadt=500)
        with self.assertRaisesRegex(ValueError, 'Unexpected Digiroad feature dr_liikennemaara.102'):
            digiroad.fetch(self.sleeps.append)
        self.wfs.features[2] = road(2)
        for feature in self.wfs.features:
            del feature['properties']['muokkauspv']
        with self.assertRaisesRegex(ValueError, 'no edit times'):
            digiroad.fetch(self.sleeps.append)

    def test_a_failed_page_waits_once_then_stops(self):
        self.wfs.failing = 1
        self.assertEqual(len(digiroad.fetch(self.sleeps.append)[0]), 8)
        self.assertIn(digiroad.BACK_OFF, self.sleeps)
        self.wfs.failing = 2
        with self.assertRaisesRegex(RuntimeError, 'failed twice at feature 0'):
            digiroad.fetch(self.sleeps.append)

    def tiles(self, directory):
        with open(directory / digiroad.ARCHIVE, 'rb') as f:
            return {xyz: json.loads(gzip.decompress(data)) for xyz, data in all_tiles(MmapSource(f))}

    def test_the_release_has_each_road_in_every_tile_it_touches(self):
        roads, edited = digiroad.fetch(self.sleeps.append)
        manifest = digiroad.build(roads, edited, self.tmp / 'release', T0)
        tiles = self.tiles(self.tmp / 'release')
        self.assertEqual({z for z, _, _ in tiles}, {11})
        self.assertEqual(sorted(xyz for xyz, t in tiles.items() if any(e['id'] == 100 for e in t['elements'])),
                         [(11, 1159, 577), (11, 1160, 577)])
        self.assertTrue(all(t['schema'] == 1 for t in tiles.values()))
        traffic = manifest['datasets']['traffic']
        self.assertEqual((traffic['format'], traffic['zoom'], traffic['features'], traffic['tiles']), ('pmtiles-json', 11, 8, len(tiles)))
        self.assertTrue(manifest['release'].startswith('20261123T180000Z-'))
        self.assertIn('Väylävirasto', manifest['attribution'])
        self.assertIn('creativecommons.org/licenses/by/4.0', (self.tmp / 'release' / 'LICENSE.txt').read_text())
        publish.local_release(self.tmp / 'release')
        again = digiroad.build(roads, edited, self.tmp / 'again', T0)
        self.assertEqual(again['files'], manifest['files'], 'the same roads give the same bytes')

    def test_the_fixture_is_the_archive_the_wfs_answer_gives(self):
        fixtures = Path(__file__).resolve().parents[1] / 'fixtures'
        self.wfs.features = json.loads((fixtures / 'digiroad-tampere.json').read_text())['features']
        with patch.object(digiroad, 'PAGE', 5000):
            manifest = digiroad.build(*digiroad.fetch(self.sleeps.append), self.tmp / 'release', T0)
        self.assertEqual((self.tmp / 'release' / digiroad.ARCHIVE).read_bytes(), (fixtures / 'digiroad-traffic.pmtiles').read_bytes())
        self.assertEqual(manifest['datasets']['traffic']['features'], 89)

    def test_the_release_goes_up_in_cells_beside_polku_osm(self):
        fetched = digiroad.fetch(self.sleeps.append)
        host = RoutingHost()
        host.dirs.add(BASE)
        host.files[BASE + '/.htaccess'] = b'installed'
        with patch.dict('os.environ', ENV | {'OSM_CELLS': 'true'}, clear=True), \
                patch.object(publish, 'open_sftp', host.ftp), patch.object(publish.urllib.request, 'urlopen', host.http):
            digiroad.build(*fetched, self.tmp / 'release', T0)
            publish.upload(self.tmp / 'release', folder=digiroad.FOLDER)
            raw = host.files[BASE + '/candidates/current.json']
            manifest = json.loads(raw)
            traffic = manifest['datasets']['traffic']
            self.assertEqual(traffic['format'], publish.CELLS)
            self.assertEqual(host.files[f"{BASE}/releases/{manifest['release']}/manifest.json"], raw)
            index = host.files[f"{BASE}/{publish.place(manifest['release'], publish.index_entry(traffic) | {'url': traffic['url']})}"]
            for cell in publish.cells_of(index, publish.index_entry(traffic)):
                self.assertIn(f"{BASE}/files/{cell['sha256']}.pmtiles", host.files)
            self.assertFalse(any(p.startswith(SITE + '/polku-osm/') for p in host.files), 'nothing in polku-osm')
            host.uploads.clear()
            digiroad.build(*fetched, self.tmp / 'again', datetime(2026, 11, 24, tzinfo=timezone.utc))
            publish.upload(self.tmp / 'again', folder=digiroad.FOLDER)
            self.assertEqual([p for p, _, _ in host.uploads if p.endswith('.pmtiles')], [], 'the same roads send no cell again')
            del host.files[BASE + '/.htaccess']
            with self.assertRaisesRegex(RuntimeError, 'run install-hosting first'):
                publish.upload(self.tmp / 'again', folder=digiroad.FOLDER)


if __name__ == '__main__':
    unittest.main()
