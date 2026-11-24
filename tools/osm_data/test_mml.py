"""The copy of NLS's background map against a fake MML and the routing tests' in-memory host."""

from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pmtiles.reader import MmapSource, Reader, all_tiles
from pmtiles.writer import Writer
from pmtiles.tile import Compression, TileType, tileid_to_zxy, zxy_to_tileid

from tools.osm_data import mml, publish
from tools.osm_data.test_publish import ENV, PUBLIC
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
    """MML's answers by tile: a status, or an exception, in turn; once they run out, 304 to If-Modified-Since LM for a
    tile not in `changed`, else 200 with the tile's body, a `changed` tile's with other layers."""
    def __init__(self, answers=None, changed=()):
        self.answers, self.changed, self.asked, self.since, self.lock = answers or {}, changed, [], {}, threading.Lock()

    def __call__(self, z, x, y, since=None):
        with self.lock:
            self.asked.append((z, x, y))
            self.since[(z, x, y)] = since
            turn = self.answers.get((z, x, y))
            answer = turn.pop(0) if turn else 304 if since == LM and (z, x, y) not in self.changed else 200
        if isinstance(answer, Exception):
            raise answer
        data = (vector('liikenne', f'{z}/{x}/{y}') if (z, x, y) in self.changed else body(z, x, y)) if answer == 200 else b''
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

    def test_an_update_asks_with_the_tiles_dates_and_keeps_a_304_as_the_bases_tile(self):
        undated = TILES[0]
        base = SimpleNamespace(sha256='a' * 64, dated=lambda: {mml.zxy_to_tileid(*t): (200, LM) for t in TILES if t != undated})
        fake = FakeMML({TILES[1]: [200]}, changed={TILES[2]})
        self.fetch(fake, base=base)
        rows = self.rows()
        self.assertIsNone(fake.since[undated], 'a tile the table has no date for is asked plainly')
        self.assertEqual(fake.since[TILES[3]], LM)
        self.assertEqual(rows[TILES[3]], (200, LM, None), "a 304 keeps the base's status and date, not its data")
        self.assertEqual(gzip.decompress(rows[TILES[2]][2]), vector('liikenne', '{}/{}/{}'.format(*TILES[2])))
        with self.assertRaisesRegex(ValueError, 'The store is for the base'):
            self.fetch(FakeMML())

    def test_a_304_mml_was_not_asked_for_is_trouble(self):
        fake = FakeMML({TILES[0]: [304]})
        self.fetch(fake)
        self.assertEqual(fake.asked.count(TILES[0]), 2)
        self.assertIsNotNone(self.rows()[TILES[0]][2])

    def test_mml_answering_400_to_everything_stops_the_run(self):
        with patch.object(mml, 'GIVE_UP', 0), self.assertRaisesRegex(mml.Refused, 'No good answer'):
            self.fetch(FakeMML({tile: [400] for tile in TILES}))

    def test_the_hand_check_needs_helsinki_in_every_zoom(self):
        def helsinki(z, x, y):
            data = vector('nimisto', 'Helsinki')
            return 200, {'Content-Length': str(len(data))}, data, 0.1
        with patch.object(mml, 'MML', lambda key: helsinki), patch.object(mml.time, 'sleep'), redirect_stdout(io.StringIO()):
            mml.sample()
            with self.assertRaisesRegex(ValueError, "the fetch doesn't start"):
                with patch.object(mml, 'MML', lambda key: FakeMML({tile: [404] for tile in TILES})):
                    mml.sample()

    def test_a_refused_key_stops_the_run_and_keeps_what_came_before(self):
        fake = FakeMML({tile: [403] for tile in TILES[3:]})
        with self.assertRaisesRegex(mml.Refused, 'refused the key'):
            mml.fetch(self.store, BOX, request=fake, rate=1000, connections=1)
        self.assertEqual(set(self.rows()), set(TILES[:3]))
        self.assertLessEqual(len(fake.asked), 5, 'one refusal stops it, with at most one request a connection in flight')

    def test_mml_answering_only_errors_stops_the_run_and_the_key_is_never_printed(self):
        def failing(z, x, y, since=None):
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
        self.host.dirs.add(FOLDER)
        self.host.files[FOLDER + '/.htaccess'] = Path(mml.__file__).with_name('mml.htaccess').read_bytes()
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.dict('os.environ', ENV | {'OSM_CELLS': 'true'}, clear=True))
        patches.enter_context(patch.object(publish, 'open_sftp', self.host.ftp))
        patches.enter_context(patch.object(publish.urllib.request, 'urlopen', self.host.http))
        patches.enter_context(redirect_stdout(io.StringIO()))

    def pack(self, now=T0):
        out = self.tmp / now.strftime('%H%M')
        return out, mml.pack(self.store, out, now)

    def cells(self, manifest):
        """Each dataset's cells on the host, as {zoom: [shared paths]}."""
        result = {}
        for dataset in manifest['datasets'].values():
            entry = publish.index_entry(dataset)
            index = self.host.files[f"{FOLDER}/{publish.place(manifest['release'], entry | {'url': dataset['url']})}"]
            result[dataset['zoom']] = [f"{FOLDER}/{publish.place(manifest['release'], c)}" for c in publish.cells_of(index, entry)]
        return result

    def test_each_zoom_has_its_archive_holding_every_tile_as_fetched_and_the_table_their_dates(self):
        out, manifest = self.pack()
        self.assertEqual(sorted(manifest['datasets']), [f'taustakartta-{z}' for z in range(11, 15)])
        for z in range(11, 15):
            with open(out / mml.archive_name(z), 'rb') as stream:
                reader = Reader(MmapSource(stream))
                header = reader.header()
                self.assertEqual((header['tile_type'], header['tile_compression'], header['min_zoom'], header['max_zoom']),
                                 (TileType.MVT, Compression.GZIP, z, z))
                self.assertEqual(reader.metadata()['attribution'], '© Maanmittauslaitos', 'no date: it would change every cell')
                if z == 14:
                    self.assertEqual(gzip.decompress(reader.get(14, 9327, 4742)), body(14, 9327, 4742))
                if z == TILES[1][0]:
                    self.assertEqual(reader.get(*TILES[1]), mml.EMPTY)
                if z == TILES[2][0]:
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

    def test_the_release_goes_up_in_cells_and_the_same_tiles_send_no_cell_again(self):
        out, _ = self.pack()
        mml.upload(out)
        raw = self.host.files[FOLDER + '/candidates/current.json']
        manifest = json.loads(raw)
        self.assertEqual({d['format'] for d in manifest['datasets'].values()}, {publish.CELLS})
        self.assertEqual(self.host.files[f"{FOLDER}/releases/{manifest['release']}/manifest.json"], raw)
        for paths in self.cells(manifest).values():
            self.assertTrue(paths and all(path in self.host.files for path in paths))
        self.assertNotIn(SITE + '/polku-osm/candidates/current.json', self.host.files, "OSM's candidate isn't touched")
        self.host.uploads.clear()
        out, second = self.pack(T0.replace(hour=4))
        mml.upload(out)
        self.assertEqual([path for path, _, _ in self.host.uploads if path.endswith('.pmtiles')], [],
                         'the same tiles a month later: no cell goes up again')
        self.assertEqual(json.loads(self.host.files[FOLDER + '/candidates/current.json'])['release'], second['release'])

    def test_the_copy_goes_up_only_in_cells(self):
        out, _ = self.pack()
        with patch.dict('os.environ', {'OSM_CELLS': ''}), self.assertRaisesRegex(ValueError, 'set OSM_CELLS=true'):
            mml.upload(out)

    def test_a_test_box_never_replaces_a_larger_candidate(self):
        out, manifest = self.pack()
        larger = dict(manifest, bounds=[24.0, 60.0, 25.0, 61.0])
        self.host.dirs.add(FOLDER + '/candidates')
        self.host.files[FOLDER + '/candidates/current.json'] = json.dumps(larger).encode()
        with self.assertRaisesRegex(ValueError, 'more than this release'):
            mml.upload(out)
        self.assertFalse([p for p in self.host.files if '/releases/' in p or '/files/' in p], 'nothing was uploaded')

    def first_copy(self):
        """The first copy as it was promoted: one archive of every zoom in its release's folder, current.json its
        manifest's own bytes."""
        out, manifest = self.pack(T0.replace(hour=2))
        stream = io.BytesIO()
        writer = Writer(stream)
        for z in range(11, 15):
            with open(out / mml.archive_name(z), 'rb') as archive:
                for (tz, x, y), data in all_tiles(MmapSource(archive)):
                    writer.write_tile(zxy_to_tileid(tz, x, y), data)
        writer.finalize({'tile_type': TileType.MVT, 'tile_compression': Compression.GZIP}, {})
        data = stream.getvalue()
        release = '20261123T023208Z'
        archive = {'path': 'taustakartta.pmtiles', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        old = dict(manifest, release=release, built_at='2026-11-23T02:32:08Z', files=[archive, *manifest['files'][-2:]],
                   datasets={'taustakartta': {'url': archive['path'], 'bytes': archive['bytes'], 'sha256': archive['sha256']}})
        raw = json.dumps(old, sort_keys=True, separators=(',', ':')).encode()
        base = f'{FOLDER}/releases/{release}'
        self.host.dirs.update({FOLDER + '/releases', base})
        self.host.files[f'{base}/taustakartta.pmtiles'] = data
        for f in manifest['files'][-2:]:
            self.host.files[f"{base}/{f['path']}"] = (out / f['path']).read_bytes()
        self.host.files[base + '/manifest.json'] = self.host.files[FOLDER + '/current.json'] = raw
        return old

    def test_the_first_copys_pointer_is_put_in_the_osm_datas_form_and_its_release_kept(self):
        old = self.first_copy()
        out, _ = self.pack()
        mml.upload(out)
        live = json.loads(self.host.files[FOLDER + '/current.json'])
        self.assertEqual(live, publish.current(old))
        self.assertEqual(live['datasets']['taustakartta']['url'], f"releases/{old['release']}/taustakartta.pmtiles")
        self.assertIn(f"{FOLDER}/releases/{old['release']}/taustakartta.pmtiles", self.host.files, 'live: kept')
        receipt = json.loads(self.host.files[f"{FOLDER}/maintenance/r-{old['release']}.json"])
        self.assertEqual((receipt['release'], receipt['previous_release']), (old['release'], None))
        url = PUBLIC.removesuffix('/polku-osm') + '/polku-mml'
        for hour in (4, 5):  # once neither live nor the predecessor, retention removes it as any release
            (self.tmp / str(hour)).mkdir()
            found = publish.candidate(url)
            publish.promote_candidate(found['release'], found['sha256'], self.tmp / str(hour), folder=mml.FOLDER)
            mml.upload(self.pack(T0.replace(hour=hour))[0])
        self.assertFalse([p for p in self.host.files if f"/releases/{old['release']}/" in p])

    def test_nothing_is_written_without_the_publication_guard(self):
        self.first_copy()
        before = dict(self.host.files)
        with patch.dict('os.environ', {'OSM_ALLOW_PUBLISH': ''}), self.assertRaisesRegex(ValueError, 'OSM_ALLOW_PUBLISH'):
            mml.upload(self.pack()[0])
        self.assertEqual(self.host.files, before)

    def update(self, now, changed=()):
        """An update from the live release: the store, its Base and the fake MML."""
        base = mml.Base.live(self.tmp / now.strftime('live%H%M'))
        store, fake = self.tmp / now.strftime('update%H%M.sqlite'), FakeMML(changed=changed)
        mml.fetch(store, BOX, request=fake, rate=1000, connections=3, base=base)
        out = self.tmp / now.strftime('update%H%M')
        return out, mml.pack(store, out, now, base=mml.Base(base.directory)), fake

    def test_an_update_from_the_first_copy_and_then_from_cells_sends_only_the_cells_whose_tiles_changed(self):
        old = self.first_copy()
        out, first, fake = self.update(T0)
        self.assertIsNone(fake.since[TILES[2]], 'a 400 is asked again plainly')
        self.assertEqual(first['tiles'], {'200': len(TILES) - 1, '404': 1})
        with open(self.tmp / '0200' / mml.archive_name(14), 'rb') as a, open(out / mml.archive_name(14), 'rb') as b:
            self.assertEqual(Reader(MmapSource(b)).get(14, 9327, 4742), Reader(MmapSource(a)).get(14, 9327, 4742),
                             "a 304's tile is the first copy's")
        lines = gzip.decompress((out / mml.TABLE).read_bytes()).decode().splitlines()
        self.assertIn('\t'.join(map(str, TILES[3])) + f'\t200\t{LM}', lines, 'with its date')
        mml.upload(out)
        found = publish.candidate(PUBLIC.removesuffix('/polku-osm') + '/polku-mml')
        (self.tmp / 'p').mkdir()
        publish.promote_candidate(found['release'], found['sha256'], self.tmp / 'p', folder=mml.FOLDER)
        before = self.cells(json.loads(self.host.files[FOLDER + '/candidates/current.json']))
        self.host.uploads.clear()
        out, second, _ = self.update(T0.replace(hour=4), changed={TILES[0]})
        mml.upload(out)
        sent = sorted(path.rsplit('/', 1)[1] for path, _, _ in self.host.uploads if path.endswith('.pmtiles'))
        after = self.cells(json.loads(self.host.files[FOLDER + '/candidates/current.json']))
        self.assertEqual(sent, sorted(path.rsplit('/', 1)[1] for path in set(after[TILES[0][0]]) - set(before[TILES[0][0]])))
        self.assertEqual(len(sent), 1, 'the one cell holding the changed tile')
        self.assertEqual({z: after[z] for z in after if z != TILES[0][0]}, {z: before[z] for z in before if z != TILES[0][0]})
        self.assertIn(f"{FOLDER}/releases/{old['release']}/taustakartta.pmtiles", self.host.files, 'the predecessor: kept')

    def test_a_stalled_download_of_the_live_release_goes_on_from_where_it_stopped(self):
        self.first_copy()
        base = mml.Base.live(self.tmp / 'live')
        archive = base.manifest['datasets']['taustakartta']
        data, real, asked = self.host.files[FOLDER + '/' + archive['url']], publish.urllib.request.urlopen, []

        class Stalled(io.BytesIO):
            status = 200
            def read(self, size=-1):
                if self.tell() >= 100:
                    raise TimeoutError('The read operation timed out')
                return super().read(min(size if size >= 0 else 100, 100 - self.tell()))

        def urlopen(request, **kwargs):
            if not request.full_url.endswith(archive['url']):
                return real(request, **kwargs)
            asked.append(request.get_header('Range'))
            if len(asked) == 1:
                return Stalled(data)
            rest = io.BytesIO(data[100:])
            rest.status = 206
            return rest

        with patch.object(mml.urllib.request, 'urlopen', urlopen), patch.object(mml.time, 'sleep'):
            path = base.file(archive)
        self.assertEqual(asked, [None, 'bytes=100-'])
        self.assertEqual(path.read_bytes(), data)

    def test_an_update_needs_its_live_release(self):
        with self.assertRaisesRegex(ValueError, 'promote a whole copy first'):
            mml.Base.live(self.tmp / 'live')
        self.first_copy()
        base = mml.Base.live(self.tmp / 'live')
        store = self.tmp / 'update.sqlite'
        mml.fetch(store, BOX, request=FakeMML(), rate=1000, connections=3, base=base)
        with self.assertRaisesRegex(ValueError, 'An update needs its live release'):
            mml.pack(store, self.tmp / 'x', T0)
        (base.directory / 'current.json').write_bytes(self.host.files[FOLDER + '/current.json'] + b' ')
        with self.assertRaisesRegex(ValueError, 'not the one the update went on from'):
            mml.pack(store, self.tmp / 'x', T0, base=mml.Base(base.directory))

    def candidate_of(self, now):
        out, manifest = self.pack(now)
        mml.upload(out)
        found = publish.candidate(PUBLIC.removesuffix('/polku-osm') + '/polku-mml')
        self.assertEqual(found['release'], manifest['release'])
        return found

    def test_a_candidate_reaches_the_live_pointer_only_when_promoted_and_restore_puts_it_back(self):
        old = self.first_copy()
        first = self.candidate_of(T0)
        before = self.host.files[FOLDER + '/current.json']
        self.assertEqual(json.loads(before)['release'], old['release'], 'an upload never names the live release')
        (self.tmp / 'one').mkdir()
        publish.promote_candidate(first['release'], first['sha256'], self.tmp / 'one', folder=mml.FOLDER)
        raw = self.host.files[f"{FOLDER}/releases/{first['release']}/manifest.json"]
        self.assertEqual(json.loads(self.host.files[FOLDER + '/current.json']), publish.current(json.loads(raw)))
        self.assertIsNone(publish.candidate(PUBLIC.removesuffix('/polku-osm') + '/polku-mml'), 'nothing newer')
        with self.assertRaisesRegex(Exception, 'Live release changed'):
            mml.restore(self.tmp / 'one', old['release'])
        mml.restore(self.tmp / 'one', first['release'])
        self.assertEqual(self.host.files[FOLDER + '/current.json'], before, 'the first copy is live again')

    def test_only_the_checked_candidate_with_all_its_cells_is_promoted(self):
        self.first_copy()
        found = self.candidate_of(T0)
        with self.assertRaisesRegex(Exception, 'differs from the one checked'):
            publish.promote_candidate(found['release'], '0' * 64, self.tmp / 'p', folder=mml.FOLDER)
        manifest = json.loads(self.host.files[FOLDER + '/candidates/current.json'])
        self.host.files[self.cells(manifest)[14][0]] += b'x'
        with self.assertRaisesRegex(Exception, 'Indexed file is missing or differs'):
            publish.promote_candidate(found['release'], found['sha256'], self.tmp / 'p', folder=mml.FOLDER)
        self.assertEqual(json.loads(self.host.files[FOLDER + '/current.json'])['release'], '20261123T023208Z')

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
