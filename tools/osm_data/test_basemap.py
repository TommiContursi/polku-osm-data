"""OpenFreeMap's base map: the build its TileJSON names, extracted by Range from a fake planet, zoom by zoom."""

from contextlib import ExitStack
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pmtiles.reader import MmapSource, Reader
from pmtiles.tile import Compression, TileType

from tools.osm_data import basemap, extract, mml, publish
from tools.osm_data.test_routing import Response
from tools.osm_data.test_terrain import BOX, planet

T0 = datetime(2026, 12, 2, 4, 43, tzinfo=timezone.utc)
VERSION = '20261122_124456_pt'


class OpenFreeMap:
    """tiles.openfreemap.org's TileJSON, and btrfs.openfreemap.com's planet by Range."""
    def __init__(self, data):
        self.data, self.tiles = data, f'https://tiles.openfreemap.org/planet/{VERSION}/{{z}}/{{x}}/{{y}}.pbf'

    def __call__(self, request, timeout=None):
        if request.get_header('User-agent') != mml.AGENT:
            raise AssertionError('an honest User-Agent')
        if request.full_url == basemap.TILEJSON:
            return io.BytesIO(json.dumps({'tilejson': '3.0.0', 'tiles': [self.tiles]}).encode())
        assert request.full_url == basemap.PLANET.format(version=VERSION), request.full_url
        start, end = map(int, request.get_header('Range').removeprefix('bytes=').split('-'))
        return Response(self.data[start:end + 1], 206, {'Content-Range': f'bytes {start}-{end}/{len(self.data)}',
                                                        'Last-Modified': 'Sun, 22 Nov 2026 18:02:11 GMT', 'ETag': '"b"'})


class BasemapTests(unittest.TestCase):
    def setUp(self):
        self.data, self.tiles = planet((TileType.MVT, Compression.GZIP))
        self.source = OpenFreeMap(self.data)
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(urllib_request(), 'urlopen', self.source))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_the_build_its_tilejson_names_one_archive_a_zoom(self):
        manifest = basemap.build(self.tmp / 'release', BOX, range(0, 8), now=T0)
        self.assertEqual(manifest['source'], {'url': basemap.PLANET.format(version=VERSION), 'tilejson': basemap.TILEJSON,
                                              'version': VERSION, 'timestamp': '2026-11-22T18:02:11Z', 'etag': '"b"'})
        self.assertEqual(sorted(manifest['datasets']), sorted(f'basemap-{z}' for z in range(0, 8)))
        with open(self.tmp / 'release' / 'basemap-7.pmtiles', 'rb') as f:
            header = Reader(MmapSource(f)).header()
        self.assertEqual((header['tile_type'], header['tile_compression'], header['min_zoom']), (TileType.MVT, Compression.GZIP, 7))
        self.assertEqual(manifest['datasets']['basemap-7']['tiles'], sum(t in self.tiles for t in extract.ids(BOX, 7)))
        self.assertIn('Open Database License', (self.tmp / 'release' / 'LICENSE.txt').read_text())
        publish.local_release(self.tmp / 'release')

    def test_an_unexpected_tilejson_or_planet_stops_the_build(self):
        self.source.tiles = 'https://elsewhere.example/{z}/{x}/{y}.pbf'
        with self.assertRaisesRegex(ValueError, 'Unexpected OpenFreeMap tile URL'):
            basemap.build(self.tmp / 'release', BOX, range(0, 8), now=T0)
        self.source.tiles = f'https://tiles.openfreemap.org/planet/{VERSION}/{{z}}/{{x}}/{{y}}.pbf'
        self.source.data = planet()[0]  # WebP: not a base map
        with self.assertRaisesRegex(ValueError, 'no longer gzipped vector tiles'):
            basemap.build(self.tmp / 'release', BOX, range(0, 8), now=T0)


def urllib_request():
    return extract.urllib.request  # (the same module basemap.py and publish.py use)


if __name__ == '__main__':
    unittest.main()
