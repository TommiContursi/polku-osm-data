"""Mapterhorn's terrain: a box extracted by Range from a fake planet, zoom by zoom, uploaded in cells and promoted."""

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from pmtiles.reader import MemorySource, MmapSource, Reader, all_tiles
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from tools.osm_data import extract, mml, publish, terrain
from tools.osm_data.test_publish import ENV
from tools.osm_data.test_routing import SITE, RoutingHost, Response

T0 = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)
BASE = SITE + '/polku-terrain'
BOX = (0.0, 40.0, 40.0, 75.0)
ZOOMS = range(0, 8)
SEA = b'RIFF-sea-webp'  # one tile the planet stores once for many places


def planet(kind=(TileType.WEBP, Compression.NONE)):
    """A clustered planet of WebP tiles (or `kind`), zooms 0-7: every tile but a hole of missing ones, the sea stored
    once."""
    rng, tiles = random.Random(7), {}
    for z in ZOOMS:
        for x in range(2 ** z):
            for y in range(2 ** z):
                if z == 7 and 70 <= x < 74 and 30 <= y < 34:
                    continue  # no terrain there
                tiles[zxy_to_tileid(z, x, y)] = SEA if (x + y) % 5 == 0 else b'RIFF' + rng.randbytes(rng.randint(5, 60))
    out = io.BytesIO()
    writer = Writer(out)
    for tile in sorted(tiles):
        writer.write_tile(tile, tiles[tile])
    writer.finalize({'tile_type': kind[0], 'tile_compression': kind[1],
                     'min_lon_e7': -1800000000, 'min_lat_e7': -850000000, 'max_lon_e7': 1800000000, 'max_lat_e7': 850000000},
                    {'attribution': '© Mapterhorn'})
    return out.getvalue(), tiles


class Mapterhorn:
    """download.mapterhorn.com: the planet by Range, and attribution.json."""
    def __init__(self, data):
        self.data, self.etag, self.failing, self.reads = data, '"v1"', 0, []

    def __call__(self, request, timeout=None):
        if request.get_header('User-agent') != mml.AGENT:
            raise AssertionError('an honest User-Agent')
        if request.full_url == terrain.SOURCES:
            return io.BytesIO(json.dumps({'fi': {'license': 'CC BY 4.0'}}).encode())
        assert request.full_url == terrain.SOURCE
        if self.failing:
            self.failing -= 1
            raise HTTPError(request.full_url, 503, 'busy', {}, None)
        start, end = map(int, request.get_header('Range').removeprefix('bytes=').split('-'))
        self.reads.append((start, end - start + 1))
        return Response(self.data[start:end + 1], 206, {'Content-Range': f'bytes {start}-{end}/{len(self.data)}',
                                                        'Last-Modified': 'Thu, 11 Sep 2026 14:58:22 GMT', 'ETag': self.etag})


class TerrainTests(unittest.TestCase):
    def setUp(self):
        self.data, self.tiles = planet()
        self.source = Mapterhorn(self.data)
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(extract.urllib.request, 'urlopen', self.source))
        patches.enter_context(patch.object(terrain.urllib.request, 'urlopen', self.source))
        patches.enter_context(patch.object(extract, 'RUN', 4096))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def build(self, name='release', now=T0):
        return terrain.build(self.tmp / name, BOX, ZOOMS, extract.Remote(terrain.SOURCE, sleep=lambda s: None), now=now)

    def test_each_zoom_is_an_archive_of_the_boxs_tiles_byte_for_byte(self):
        manifest = self.build()
        for z in ZOOMS:
            with open(self.tmp / 'release' / f'terrain-{z}.pmtiles', 'rb') as f:
                got = {zxy_to_tileid(*zxy): data for zxy, data in all_tiles(MmapSource(f))}
                header = Reader(MmapSource(f)).header()
            wanted = {t: self.tiles[t] for t in extract.ids(BOX, z) if t in self.tiles}
            self.assertEqual(got, wanted, f'zoom {z}: every tile in the box the planet has, and no other')
            self.assertEqual((header['min_zoom'], header['max_zoom'], header['tile_type'], header['tile_compression']),
                             (z, z, TileType.WEBP, Compression.NONE))
            self.assertEqual(manifest['datasets'][f'terrain-{z}'] | {'url': None},
                             {'url': None, 'zoom': z, 'tiles': len(wanted), 'bytes': (self.tmp / 'release' / f'terrain-{z}.pmtiles').stat().st_size,
                              'sha256': publish.digest_file(self.tmp / 'release' / f'terrain-{z}.pmtiles')})
        self.assertLess(len(got), len(extract.ids(BOX, 7)), 'the hole has no tiles')
        self.assertEqual(manifest['source'], {'url': terrain.SOURCE, 'timestamp': '2026-09-11T14:58:22Z', 'etag': '"v1"'})
        self.assertEqual(json.loads((self.tmp / 'release' / 'attribution.json').read_text()), {'fi': {'license': 'CC BY 4.0'}})
        self.assertIn('mapterhorn.com/attribution', (self.tmp / 'release' / 'LICENSE.txt').read_text())
        publish.local_release(self.tmp / 'release')
        self.assertEqual(self.build('again')['files'], manifest['files'], 'the same planet gives the same bytes')

    def test_the_tiles_come_in_runs_not_one_by_one(self):
        self.build()
        tiles = sum(len(extract.ids(BOX, z)) for z in ZOOMS)
        self.assertLess(len(self.source.reads), tiles / 4)
        tile_data = Reader(MemorySource(self.data)).header()['tile_data_offset']
        self.assertTrue(all(length <= extract.RUN for start, length in self.source.reads if start >= tile_data))

    def test_a_changed_planet_or_a_wrong_answer_stops_the_extract(self):
        remote = extract.Remote(terrain.SOURCE)
        self.source.etag = '"v2"'
        with self.assertRaisesRegex(ValueError, 'changed during the extract'):
            remote.read(0, 10)
        self.source.data = b'short'
        with self.assertRaisesRegex(ValueError, '5 of 127 bytes'):
            extract.Remote(terrain.SOURCE)

    def test_a_busy_server_is_asked_again_after_a_pause(self):
        sleeps = []
        self.source.failing = 2
        extract.Remote(terrain.SOURCE, sleep=sleeps.append)
        self.assertEqual(sleeps, [extract.BACK_OFF] * 2)
        self.source.failing = extract.ATTEMPTS
        with self.assertRaisesRegex(RuntimeError, '503'):
            extract.Remote(terrain.SOURCE, sleep=sleeps.append)

    def test_the_release_goes_up_in_cells_of_webp_and_is_promoted(self):
        host = RoutingHost()
        host.dirs.add(BASE)
        host.files[BASE + '/.htaccess'] = b'installed'
        self.build()
        with patch.dict('os.environ', ENV | {'OSM_CELLS': 'true'}, clear=True), \
                patch.object(publish, 'open_sftp', host.ftp), patch.object(publish.urllib.request, 'urlopen', host.http):
            publish.upload(self.tmp / 'release', folder=terrain.FOLDER, budget=terrain.BUDGET)
            raw = host.files[BASE + '/candidates/current.json']
            manifest = json.loads(raw)
            for z in ZOOMS:
                dataset = manifest['datasets'][f'terrain-{z}']
                self.assertEqual((dataset['format'], dataset['zoom']), (publish.CELLS, z))
                index = host.files[f"{BASE}/{publish.place(manifest['release'], publish.index_entry(dataset) | {'url': dataset['url']})}"]
                for cell in publish.cells_of(index, publish.index_entry(dataset)):
                    header = Reader(MemorySource(host.files[f"{BASE}/files/{cell['sha256']}.pmtiles"])).header()
                    self.assertEqual((header['tile_type'], header['tile_compression'], header['min_zoom']),
                                     (TileType.WEBP, Compression.NONE, z))
            found = publish.candidate(publish.product_url(ENV['OSM_PUBLIC_URL'], terrain.FOLDER), first=True)
            self.assertEqual(found, {'release': manifest['release'], 'sha256': hashlib.sha256(raw).hexdigest(),
                                     'manifest': f"{publish.product_url(ENV['OSM_PUBLIC_URL'], terrain.FOLDER)}/releases/{manifest['release']}/manifest.json"},
                             'a candidate with nothing live yet')
            publish.promote_candidate(found['release'], found['sha256'], self.tmp / 'promote', folder=terrain.FOLDER)
            self.assertEqual(json.loads(host.files[BASE + '/current.json']), publish.current(manifest))
            self.assertIsNone(publish.candidate(publish.product_url(ENV['OSM_PUBLIC_URL'], terrain.FOLDER), first=True), 'the candidate is live')
            small = manifest | {'bounds': [20.0, 60.0, 21.0, 61.0]}  # a test box
            with self.assertRaisesRegex(ValueError, r'current.json covers \[0.0, 40.0, 40.0, 75.0\], more than this release'):
                publish.require_covers(terrain.FOLDER, small)


if __name__ == '__main__':
    unittest.main()
