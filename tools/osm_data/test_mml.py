"""The copy of NLS's background map against a fake MML and the routing tests' in-memory host."""

from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pmtiles.reader import MmapSource, Reader
from pmtiles.tile import Compression, TileType, tileid_to_zxy, zxy_to_tileid

from tools.osm_data import mml, publish
from tools.osm_data.test_publish import ENV
from tools.osm_data.test_routing import RoutingHost, SITE

BOX = (24.92, 60.16, 24.96, 60.18)  # central Helsinki, as the workflow's test
TILES = [tileid_to_zxy(tile) for tile in mml.tile_ids(BOX)]
LM = 'Sat, 21 Feb 2026 14:35:07 GMT'
KEY = 'abcd1234-secret-key'
T0 = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)
FOLDER = SITE + '/polku-mml'


def vector(*names):
    """A vector tile with these layers, each with its version, name and extent."""
    data = b''
    for name in names:
        layer = b'\x78\x02\x0a' + bytes([len(name)]) + name.encode() + b'\x28\x80\x20'
        data += b'\x1a' + bytes([len(layer)]) + layer
    return data


def body(z, x, y):
    return vector('maasto_alue', f'{z}/{x}/{y}')  # names its own place, so a swapped x and y shows


class FakeMML:
    """MML's answers by tile: a status, or an exception, in turn; 200 once they run out."""
    def __init__(self, answers=None):
        self.answers, self.asked, self.lock = answers or {}, [], threading.Lock()

    def __call__(self, z, x, y):
        with self.lock:
            self.asked.append((z, x, y))
            turn = self.answers.get((z, x, y))
            answer = turn.pop(0) if turn else 200
        if isinstance(answer, Exception):
            raise answer
        data = body(z, x, y) if answer == 200 else b''
        return answer, {'Last-Modified': LM, 'Content-Length': str(len(data))}, data, 0.01


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds, 4))


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.store = self.tmp / 'store.sqlite'

    def fetch(self, fake, **kwargs):
        with redirect_stdout(io.StringIO()) as out:
            try:
                return mml.fetch(self.store, BOX, request=fake, rate=1000, connections=3, **kwargs)
            finally:
                self.output = out.getvalue()

    def rows(self):
        db = mml.store(self.store)
        try:
            return {tileid_to_zxy(tile): (status, modified, data)
                    for tile, status, modified, data in db.execute('SELECT * FROM tiles')}
        finally:
            db.close()

    def test_finlands_box_is_the_plans_892856_tiles_with_helsinki_in_it(self):
        ids = mml.tile_ids(mml.BOX)
        self.assertEqual(len(ids), 892_856)
        self.assertIn(zxy_to_tileid(14, 9327, 4742), set(ids))
        self.assertIn((14, 9327, 4742), TILES)
        self.assertEqual({z for z, _, _ in TILES}, {11, 12, 13, 14})

    def test_only_a_vector_tile_is_kept(self):
        self.assertEqual(mml.layers(vector('korkeus', 'nimisto')), ['korkeus', 'nimisto'])
        self.assertEqual(mml.layers(b''), [])
        for bad in (b'<html>busy</html>', gzip.compress(vector('a')), vector('a')[:-1], b'\x0a\x01a'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                mml.layers(bad)
        tile = vector('a')
        self.assertEqual(mml.answer(200, {'Content-Length': str(len(tile))}, tile), ('tile', gzip.compress(tile, 6, mtime=0)))
        for status, headers, data, kind in [
                (200, {}, gzip.compress(tile), 'trouble'), (200, {'Content-Encoding': 'gzip'}, tile, 'trouble'),
                (200, {'Content-Length': '99'}, tile, 'trouble'), (200, {}, b'<html>', 'trouble'),
                (404, {}, b'', 'empty'), (400, {}, b'', 'recorded'), (410, {}, b'', 'recorded'),
                (401, {}, b'', 'refused'), (403, {}, b'', 'refused'), (429, {}, b'', 'trouble'),
                (503, {}, b'', 'trouble'), (302, {}, b'', 'trouble')]:
            with self.subTest(status=status, headers=headers):
                self.assertEqual(mml.answer(status, headers, data)[0], kind)

    def test_the_pace_is_even_halves_on_trouble_and_rises_slowly(self):
        clock = Clock()
        pace = mml.Pace(40, clock, clock.sleep)
        for _ in range(3):
            pace.wait()
        self.assertEqual(clock.slept, [0.025, 0.05])
        pace.trouble()
        pace.trouble()  # the same burst of failures halves once
        self.assertEqual(pace.rate, 20)
        for _ in range(6):
            clock.now += 5
            pace.trouble()
        self.assertEqual((pace.rate, pace.floor_since), (mml.FLOOR, 25))
        clock.now += mml.CALM
        pace.next = clock.now
        pace.wait()
        self.assertEqual((pace.rate, pace.floor_since), (mml.FLOOR + mml.RAISE, None))
        clock.now += 60
        pace.trouble(pause=120)
        self.assertEqual(pace.next, clock.now + 120, "429's Retry-After holds every connection back")

    def test_every_tile_is_asked_once_and_kept_with_what_mml_said(self):
        empty, wrong, busy, slow = TILES[1], TILES[2], TILES[3], TILES[4]
        fake = FakeMML({empty: [404], wrong: [400], busy: [503, 429], slow: [TimeoutError(), 200]})
        report = self.fetch(fake)
        rows = self.rows()
        self.assertEqual(set(rows), set(TILES))
        self.assertEqual(rows[empty][0], 404)
        self.assertIsNone(rows[empty][2])
        self.assertEqual(rows[wrong][:1], (400,))
        self.assertEqual(rows[TILES[0]], (200, LM, gzip.compress(body(*TILES[0]), 6, mtime=0)))
        self.assertEqual(sorted(fake.asked), sorted(TILES + [busy, busy, slow]), 'only trouble is asked again')
        self.assertEqual((report['tile'], report['empty'], report['recorded']), (len(TILES) - 2, 1, 1))

    def test_a_stopped_run_goes_on_without_asking_again(self):
        self.fetch(FakeMML(), limit=3)
        self.assertEqual(len(self.rows()), 3)
        report = self.fetch(FakeMML(), until=0)
        self.assertEqual((len(self.rows()), report['left']), (3, len(TILES) - 3), 'past the deadline it asks nothing')
        fake = FakeMML()
        self.fetch(fake)
        self.assertEqual(len(fake.asked), len(TILES) - 3)
        self.assertEqual(set(self.rows()), set(TILES))
        with self.assertRaisesRegex(ValueError, 'box'):
            mml.fetch(self.store, mml.BOX, request=FakeMML())

    def test_a_deadline_hands_back_the_rest_at_once_not_at_the_paced_rate(self):
        with redirect_stdout(io.StringIO()):
            start = time.monotonic()
            report = mml.fetch(self.store, BOX, until=time.time() + 0.5, request=FakeMML(), rate=1, connections=2)
        self.assertLess(time.monotonic() - start, 3, 'ten tiles at one a second would take ten')
        self.assertEqual(report['tile'] + report['left'], len(TILES))
        self.assertGreater(report['left'], 0)

    def test_a_refused_key_stops_the_run_and_keeps_what_came_before(self):
        fake = FakeMML({tile: [403] for tile in TILES[3:]})
        with self.assertRaisesRegex(mml.Refused, 'refused the key'):
            mml.fetch(self.store, BOX, request=fake, rate=1000, connections=1)
        self.assertEqual(set(self.rows()), set(TILES[:3]))
        self.assertLessEqual(len(fake.asked), 5, 'one refusal stops it, with at most one request a connection in flight')

    def test_mml_answering_only_errors_stops_the_run_and_the_key_is_never_printed(self):
        def failing(z, x, y):
            raise OSError(f'https://{mml.HOST}/x?api-key={KEY}')
        with patch.object(mml, 'GIVE_UP', 0), self.assertRaisesRegex(mml.Refused, 'No good answer'):
            self.fetch(failing)
        self.assertNotIn(KEY, self.output)
        self.assertIn('OSError', self.output)
        self.assertEqual(self.rows(), {})
        self.assertIn(KEY, mml.MML(KEY).query)
        with self.assertRaisesRegex(ValueError, 'MML_API_KEY'):
            mml.MML('')


class PackAndUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.store = self.tmp / 'store.sqlite'
        with redirect_stdout(io.StringIO()):
            mml.fetch(self.store, BOX, request=FakeMML({TILES[1]: [404], TILES[2]: [400]}), rate=1000, connections=3)
        self.host = RoutingHost()
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.dict('os.environ', ENV, clear=True))
        patches.enter_context(patch.object(publish, 'open_sftp', self.host.ftp))
        patches.enter_context(patch.object(publish.urllib.request, 'urlopen', self.host.http))
        patches.enter_context(redirect_stdout(io.StringIO()))

    def pack(self, now=T0):
        out = self.tmp / now.strftime('%H%M')
        return out, mml.pack(self.store, out, now)

    def test_the_archive_holds_every_tile_as_fetched_and_the_table_their_dates(self):
        out, manifest = self.pack()
        with open(out / mml.ARCHIVE, 'rb') as stream:
            reader = Reader(MmapSource(stream))
            header = reader.header()
            self.assertEqual((header['tile_type'], header['tile_compression'], header['min_zoom'], header['max_zoom']),
                             (TileType.MVT, Compression.GZIP, 11, 14))
            self.assertEqual(gzip.decompress(reader.get(14, 9327, 4742)), body(14, 9327, 4742))
            self.assertEqual(reader.get(*TILES[1]), mml.EMPTY)
            self.assertIsNone(reader.get(*TILES[2]), 'another 4xx is left to MML')
        lines = gzip.decompress((out / mml.TABLE).read_bytes()).decode().splitlines()
        self.assertEqual(len(lines), len(TILES) + 1)
        self.assertIn('\t'.join(map(str, TILES[0])) + f'\t200\t{LM}', lines)
        self.assertEqual(manifest['attribution'], '© Maanmittauslaitos, Maastotietokanta 11/2026')
        self.assertEqual(manifest['tiles'], {'200': len(TILES) - 2, '404': 1, '400': 1})
        self.assertEqual((manifest['zooms'], manifest['bounds']), ([11, 14], list(BOX)))
        self.assertNotIn('api-key', (out / 'manifest.json').read_text())
        publish.local_release(out)

    def test_a_store_with_tiles_missing_is_not_packed(self):
        db = mml.store(self.store)
        db.execute('DELETE FROM tiles WHERE id = (SELECT MAX(id) FROM tiles)')
        db.commit()
        db.close()
        with self.assertRaisesRegex(ValueError, '1 tiles have no answer yet'):
            self.pack()

    def test_the_first_upload_installs_the_folder_and_names_the_candidate(self):
        out, manifest = self.pack()
        mml.upload(out)
        raw = (out / 'manifest.json').read_bytes()
        self.assertEqual(self.host.files[FOLDER + '/.htaccess'], Path(mml.__file__).with_name('mml.htaccess').read_bytes())
        self.assertEqual(self.host.files[FOLDER + '/candidates/current.json'], raw)
        base = f"{FOLDER}/releases/{manifest['release']}"
        self.assertEqual(self.host.files[base + '/' + mml.ARCHIVE], (out / mml.ARCHIVE).read_bytes())
        self.assertFalse([p for p in self.host.files if '/staging' in p])
        self.assertNotIn(SITE + '/polku-osm/candidates/current.json', self.host.files, "OSM's candidate isn't touched")

    def test_a_stopped_upload_goes_on_from_where_it_stopped(self):
        out, manifest = self.pack()
        archive = (out / mml.ARCHIVE).read_bytes()
        self.host.dirs.add(FOLDER)
        self.host.dirs.add(FOLDER + '/staging')
        self.host.files[FOLDER + '/.htaccess'] = Path(mml.__file__).with_name('mml.htaccess').read_bytes()
        self.host.files[FOLDER + '/staging/_run.json'] = json.dumps({'release': manifest['release']}).encode()
        self.host.files[FOLDER + '/staging/' + mml.ARCHIVE] = archive[:100]
        mml.upload(out)
        self.assertIn((FOLDER + '/staging/' + mml.ARCHIVE, 100, len(archive) - 100), self.host.uploads)
        self.assertEqual(self.host.files[f"{FOLDER}/releases/{manifest['release']}/{mml.ARCHIVE}"], archive)

    def test_a_resumed_file_that_differs_is_uploaded_whole_next_time(self):
        out, manifest = self.pack()
        self.host.dirs.update({FOLDER, FOLDER + '/staging'})
        self.host.files[FOLDER + '/staging/_run.json'] = json.dumps({'release': manifest['release']}).encode()
        self.host.files[FOLDER + '/staging/' + mml.ARCHIVE] = b'X' * 100
        with self.assertRaisesRegex(RuntimeError, 'SHA-256 differs'):
            mml.upload(out)
        self.assertNotIn(FOLDER + '/staging', self.host.dirs)
        mml.upload(out)
        self.assertIn(FOLDER + '/candidates/current.json', self.host.files)

    def test_a_new_candidate_replaces_the_old_one_but_never_the_live_one(self):
        first = self.pack()[1]['release']
        mml.upload(self.tmp / '0300')
        self.host.files[FOLDER + '/current.json'] = self.host.files[f'{FOLDER}/releases/{first}/manifest.json']
        out, second = self.pack(T0.replace(hour=4))
        mml.upload(out)
        out, third = self.pack(T0.replace(hour=5))
        mml.upload(out)
        releases = {p.split('/')[-2] for p in self.host.files if '/releases/' in p}
        self.assertEqual(releases, {first, third['release']})

    def test_a_test_box_never_replaces_a_larger_candidate(self):
        out, manifest = self.pack()
        larger = dict(manifest, bounds=[24.0, 60.0, 25.0, 61.0])
        self.host.dirs.update({FOLDER, FOLDER + '/candidates'})
        self.host.files[FOLDER + '/candidates/current.json'] = json.dumps(larger).encode()
        with self.assertRaisesRegex(RuntimeError, 'more than this release'):
            mml.upload(out)
        self.assertFalse([p for p in self.host.files if '/staging' in p or '/releases/' in p], 'nothing was uploaded')

    def test_hosting_that_was_changed_by_hand_stops_the_upload(self):
        out, _ = self.pack()
        self.host.dirs.add(FOLDER)
        self.host.files[FOLDER + '/.htaccess'] = b'changed by hand'
        with self.assertRaisesRegex(RuntimeError, 'change it by hand'):
            mml.upload(out)

    def test_the_fetch_never_holds_the_hosts_secrets_and_only_main_uploads(self):
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/mml-data.yml').read_text()
        fetch, rest = workflow.split('\n  pack:')
        pack, upload = rest.split('\n  upload:')
        self.assertNotIn('secrets.OSM_', fetch + pack)
        self.assertNotIn('MML_API_KEY', pack + upload)
        self.assertIn("github.ref == 'refs/heads/main'", upload)
        self.assertIn('environment: osm-data-production', upload)


if __name__ == '__main__':
    unittest.main()
