"""The search index: records from a small Photon dump, NLS's names and GeoNames' towns, laid out in groups, built on
the previous release, uploaded beside polku-osm and promoted."""

from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools.osm_data import publish, search
from tools.osm_data.test_publish import ENV
from tools.osm_data.test_routing import SITE, RoutingHost

T0 = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)
BASE = SITE + '/polku-search'
FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def place(i, name, key='place', value='village', lon=25.0, lat=61.0, importance=0.2, kind='N', **extra):
    return {'object_type': kind, 'object_id': i, 'osm_key': key, 'osm_value': value, 'importance': importance,
            'name': {'name': name} if isinstance(name, str) else name, 'country_code': 'fi', 'centroid': [lon, lat],
            'address': {'city': 'Kotka'}, **extra}


def dump(places, timestamp='2026-11-22T00:02:21.000+00:00'):
    head = {'type': 'NominatimDumpFile', 'content': {'data_timestamp': timestamp}}
    countries = {'type': 'CountryInfo', 'content': [{'country_code': 'se', 'name': {'name': 'Sverige', 'name:fi': 'Ruotsi'}},
                                                    {'country_code': 'fi', 'name': {'name': 'Suomi'}}]}
    return iter([json.dumps(head), json.dumps(countries)] + [json.dumps({'type': 'Place', 'content': [p]}) for p in places])


def records(rows):
    return {r['id']: r for r in rows}


def rec(i, name, lon=25.0, lat=61.0, rank=0.2, kind='place/village'):
    return {'id': i, 'names': [name] if isinstance(name, str) else name, 'kind': kind, 'lon': lon, 'lat': lat,
            'rank': rank, 'city': ''}


def find(directory, key, area=None, dirs=()):
    """Look a key up in a release folder (and the folders of its bases), as Polku does."""
    read = search.local_reader(directory, *dirs)
    root = search.read_index(read(search.root_of(json.loads((Path(directory) / 'manifest.json').read_text()))))
    row = next((r for r in root['groups'] if r[0] == search.group(key)), None)
    if row is None:
        return {}, len(key) > 1  # no such group: nothing, completely, unless the key is one letter
    body = search.read_index(read({'path': row[2] + search.SUFFIX, 'bytes': row[1], 'sha256': row[2]}))
    return lookup(body, lambda r: gzip.decompress(read({'path': r[3] + search.SUFFIX, 'sha256': r[3]})).decode(),
                         key, area)


def matches(text, key):
    """Whether a record line has a key that starts with key."""
    return any(k.startswith(key) for k in search.keys(text.split('\t', 2)[1].split('|')))


def lookup(body, read, key, area=None):
    """What a query's key reaches in its group, as a reader reads it: {id: line} of the records with a key starting with
    key, and whether that is all of them. body is the group's index; read(row) gives a file's text; area is the tile
    (AREA) a common word split by area is read for."""
    rows = {(prefix, kind): row for row in body['files'] for prefix, kind, _, _ in [row]}
    prefix, texts, complete = search.group(key), [], len(key) > 1  # a letter's group holds only that word: k's are in ka, kb…
    while True:
        if (prefix, 'L') in rows:
            texts.append(read(rows[prefix, 'L']))
            break
        if (prefix, 'T') not in rows:
            break  # no key starts with it
        if key == prefix:  # a prefix this short reaches more than one file: its most important, and the word itself
            texts.append(read(rows[prefix, 'T']))
            if (prefix, 'W') in rows:
                texts.append(read(rows[prefix, 'W']))
            elif area and (f'{prefix}@{area}', 'A') in rows:
                texts.append(read(rows[f'{prefix}@{area}', 'A']))
            complete = False
            break
        prefix = key[:len(prefix) + 1]
    replaced = set(body['removed']) | {l.split('\t', 1)[0] for l in body['changed']}
    found = {}
    for l in [l for t in texts for l in t.splitlines() if l.split('\t', 1)[0] not in replaced] + body['changed']:
        if matches(l, key):
            found[l.split('\t', 1)[0]] = l
    return found, complete


class KeyTests(unittest.TestCase):
    def test_the_shared_cases(self):
        for text, expected in json.loads((FIXTURES / 'search-normalise.json').read_text())['cases']:
            self.assertEqual(search.words(text), expected, text)

    def test_a_name_of_several_words_is_also_one_key(self):
        self.assertEqual(search.keys(['Iso Haukijärvi', 'Koli']), {'iso', 'haukijarvi', 'isohaukijarvi', 'koli'})


class SourceTests(unittest.TestCase):
    def test_photon_keeps_named_places_merges_streets_and_names_countries(self):
        places = [place(1, {'name': 'Nuuksio', 'name:sv': 'Noux', 'alt_name': 'Nuuksio;Nuuksion kansallispuisto'},
                        'boundary', 'national_park', bbox=[24.4, 60.4, 24.6, 60.2]),
                  place(2, 'Kotikatu', 'highway', 'residential', 26.9, 60.46, 0.05, 'W'),
                  place(3, 'Kotikatu', 'highway', 'residential', 26.91, 60.47, 0.06, 'W'),
                  place(8, 'Kotikatu', 'highway', 'bus_stop', 26.9, 60.46, 0.01),
                  place(4, 'Kahvila', 'amenity', 'cafe', importance=0.01),
                  place(4, 'Kahvila', 'shop', 'pastry', importance=0.02),
                  place(5, '48100', 'place', 'postcode', kind='X'),
                  place(6, {}, 'building', 'yes', housenumber='3'),
                  {**place(7, 'Haaparanta'), 'country_code': 'se'}]
        found, timestamp = search.photon(dump(places))
        self.assertEqual(timestamp, '2026-11-22T00:02:21.000+00:00')
        self.assertEqual(found['N1']['names'], ['Nuuksio', 'Noux', 'Nuuksion kansallispuisto'])
        self.assertEqual(found['N1']['extent'], (24.4, 60.2, 24.6, 60.4))
        street = found['SW2']
        self.assertEqual((street['names'], round(street['lon'], 3), street['rank']), (['Kotikatu'], 26.905, 0.06))
        self.assertNotIn('W3', found)
        self.assertEqual(found['N8']['kind'], 'highway/bus_stop', 'a bus stop of the same name is not the street')
        self.assertEqual(found['N4']['kind'], 'shop/pastry', 'one record an object, under its most important class')
        self.assertIn('P48100', found)
        self.assertEqual(sorted(i for i in found if i[0] == 'C'), ['Cse'], 'countries beyond Finland')
        self.assertEqual(found['Cse']['names'], ['Sverige', 'Ruotsi'])
        self.assertEqual(len(found), 6, 'no address without a name, nothing beyond Finland but the countries')

    def test_nls_adds_the_places_osm_lacks_with_their_sami_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nls.jsonl.gz'
            with gzip.open(path, 'wt', encoding='utf-8') as f:
                for row in ([1, 'Inarinjärvi', 'fin', 2010299, 1000000, '148', 27.9, 69.0],
                            [1, 'Aanaarjävri', 'smn', 2010299, 1000000, '148', 27.9, 69.0],
                            [1, 'Anárjávri', 'sme', 2010299, 1000000, '148', 27.9, 69.0],
                            [2, 'Koli', 'fin', 1010210, 250000, '422', 29.98, 63.1]):
                    f.write(json.dumps(row) + '\n')
            names = search.nls(path)
        self.assertEqual(names[1]['names'], ['Inarinjärvi', 'Anárjávri', 'Aanaarjävri'])
        self.assertEqual((names[1]['kind'], names[1]['rank']), ('natural/water', 0.17), 'a type by its first digit')
        self.assertEqual(names[2]['kind'], 'natural/hill')
        osm = records([rec('N9', 'Koli', 29.97, 63.09)])
        merged = search.merge(osm, names)
        self.assertEqual(sorted(merged), ['L1', 'N9'], "NLS's Koli is OSM's")

    def test_nls_pages_are_followed_and_deleted_names_left_out(self):
        def feature(i, deleted=None):
            return {'properties': {'placeId': i, 'spelling': f'Paikka {i}', 'language': 'fin', 'placeType': 1010305,
                                   'scaleRelevance': 25000, 'municipality': '091', 'placeNameDeletionTime': deleted,
                                   'placeNameModificationTime': f'2026-0{i}-01T00:00:00Z'},
                    'geometry': {'type': 'Point', 'coordinates': [25.0, 60.0 + i / 10]}}
        pages = {'first': {'features': [feature(1), feature(2, '2026-01-01')], 'links': [{'rel': 'next', 'href': 'second'}]},
                 'second': {'features': [feature(3)], 'links': [{'rel': 'self', 'href': 'second'}]}}
        asked, sleeps = [], []

        def page(url, key, sleep):
            asked.append(url)
            return pages['first' if url.startswith(search.NLS) else url]
        with tempfile.TemporaryDirectory() as tmp, patch.object(search, 'PAGE', 2):
            with self.assertRaisesRegex(ValueError, 'NLS gave only 2 place names'):
                search.fetch_nls(Path(tmp) / 'nls.jsonl.gz', 'a-test-key', page, sleeps.append)
            with gzip.open(Path(tmp) / 'nls.jsonl.gz', 'rt') as f:
                self.assertEqual([json.loads(l)[0] for l in f], [1, 3])
        self.assertEqual(asked[1], 'second')
        self.assertEqual(sleeps, [search.PAUSE])
        with self.assertRaisesRegex(ValueError, 'MML_API_KEY is missing'):
            search.fetch_nls('x', None, page)

    def test_towns_beyond_finland_with_their_finnish_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cities15000.zip'
            rows = ['2673730\tStockholm\tStockholm\tEstocolmo,STO,Stockholm,Tukholma,Стокгольм\t59.33\t18.06\tP\tPPLC\tSE'
                    + '\t' * 6 + '1515017' + '\t' * 4,
                    '2711537\tGöteborg\tGoteborg\tGothenburg\t57.7\t11.97\tP\tPPLA\tSE' + '\t' * 6 + '99999' + '\t' * 4,
                    '658225\tHelsinki\tHelsinki\tHelsingfors\t60.17\t24.94\tP\tPPLC\tFI' + '\t' * 6 + '558457' + '\t' * 4]
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('cities15000.txt', '\n'.join(rows) + '\n')
            found = search.towns(path)
        self.assertEqual(list(found), ['G2673730', 'G2711537'])
        self.assertEqual(found['G2673730']['names'], ['Stockholm', 'Estocolmo', 'Tukholma'])
        self.assertEqual(found['G2711537']['names'], ['Göteborg', 'Goteborg'], "a smaller town's own names only")

    def test_a_source_cut_short_is_refused(self):
        previous = {'source': {'photon': {'records': 1000}, 'nls': {'names': 800}, 'geonames': {'url': 'x'}}}
        now = {'photon': {'records': 950}, 'nls': {'names': 800}, 'geonames': {'records': 10}}
        search.require_counts(now, previous)
        now['nls']['names'] = 700
        with self.assertRaisesRegex(ValueError, 'nls gave 700 names, the previous release 800'):
            search.require_counts(now, previous)

    def test_an_old_dump_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / 'sources.json').write_text(json.dumps({'photon': {}}))
            with patch.object(search, 'dump_lines', lambda path: dump([], '2026-11-01T00:00:00+00:00')):
                with self.assertRaisesRegex(ValueError, 'The Photon dump is 22 days old'):
                    search.gather(work, T0)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = patch.object(search, 'TARGET', 600)  # a few records a file
        patcher.start()
        self.addCleanup(patcher.stop)
        self.places = records([rec(f'N{i}', f'Koski{"abcdefgh"[i % 8]}{i} Lake', 25 + i / 100, 61, round(i / 1000, 2))
                               for i in range(120)]
                              + [rec(f'W{i}', 'Koski', 20 + i / 10 * (i % 2) + i % 2 * 9, 60 + i % 2 * 5, 0.1)
                                 for i in range(60)]
                              + [rec('N500', 'Iso Haukijärvi', 28, 62, 0.3), rec('Cse', ['Sverige', 'Ruotsi'], rank=0.9)])

    def build(self, name, places, base=None, now=T0):
        return search.build(places, {'photon': {}}, self.tmp / name, base, now)

    def everything_is_found(self, directory, places, dirs=()):
        for r in places.values():
            for k in search.keys(r['names']):
                found, complete = find(directory, k, search.tile(r['lon'], r['lat']), dirs)
                self.assertIn(r['id'], found, (k, complete))

    def test_every_record_is_found_by_every_key(self):
        manifest, counts = self.build('one', self.places)
        self.assertEqual(counts, {'kept': 0, 'made': 0, 'new': manifest['datasets']['search']['groups']})
        self.everything_is_found(self.tmp / 'one', self.places)
        found, complete = find(self.tmp / 'one', 'koskia')
        self.assertTrue(complete and set(found) == {f'N{i}' for i in range(0, 120, 8)}, 'a long key reads one whole file')
        found, complete = find(self.tmp / 'one', 'kos')
        self.assertFalse(complete, 'a short key reads a list of the most important')
        self.assertEqual(len(found), search.TOP)
        here, complete = find(self.tmp / 'one', 'koski', search.tile(29, 65))
        self.assertFalse(complete, 'a common word split by area reads its part')
        self.assertIn('W1', here, "this area's part")
        self.assertLess(len(set(here) & {f'W{i}' for i in range(0, 60, 2)}), search.TOP, "only the others' most important")
        self.assertEqual(find(self.tmp / 'one', 'isoh')[0].keys(), {'N500'})
        self.assertEqual(find(self.tmp / 'one', 'qq'), ({}, True), 'no such group: nothing, completely')
        self.assertEqual(find(self.tmp / 'one', 'koskix'), ({}, True))
        self.assertFalse(find(self.tmp / 'one', 'k')[1], "a letter's group holds only the word itself")
        publish.local_release(self.tmp / 'one')

    def test_the_next_release_keeps_the_files_and_lists_what_changed(self):
        first, _ = self.build('one', self.places)
        base = search.read_base(search.local_reader(self.tmp / 'one'), first)
        now = json.loads(json.dumps(self.places))
        now['N3']['names'] = ['Vuokatti']  # renamed: no longer under ko
        now['N4']['rank'] = 0.5
        del now['N5']
        now['N900'] = rec('N900', 'Koskiaho', 25, 61)
        second, counts = self.build('two', now, base, datetime(2026, 11, 30, tzinfo=timezone.utc))
        self.assertEqual((counts['made'], counts['new']), (0, 1), 'only Vuokatti\'s group is new')
        new = {p.name for p in (self.tmp / 'two').iterdir()} - {p.name for p in (self.tmp / 'one').iterdir()}
        self.assertEqual(len(self.changed_indexes(first, second)), 3, 'ko, la (Lake) and the new vu')
        self.assertEqual(len(new - self.changed_indexes(first, second) - {'manifest.json'}), 1, "and vu's one file")
        self.everything_is_found(self.tmp / 'two', now, [self.tmp / 'one'])
        for key in ('koskid3', 'koski'):
            self.assertNotIn('N3', find(self.tmp / 'two', key, search.tile(25.03, 61), [self.tmp / 'one'])[0], 'not by its old name')
        self.assertNotIn('N5', find(self.tmp / 'two', 'koskif5', None, [self.tmp / 'one'])[0])
        self.assertEqual(find(self.tmp / 'two', 'vuokatti', None, [self.tmp / 'one'])[0].keys(), {'N3'})
        # a third build lists everything since the files again, not since the second
        base = search.read_base(search.local_reader(self.tmp / 'two', self.tmp / 'one'), second)
        self.build('three', now, base, datetime(2026, 12, 7, tzinfo=timezone.utc))
        self.everything_is_found(self.tmp / 'three', now, [self.tmp / 'two', self.tmp / 'one'])
        self.assertLessEqual({p.name for p in (self.tmp / 'three').iterdir()}, {p.name for p in (self.tmp / 'two').iterdir()},
                             'nothing changed: the same root and group indexes')

    def changed_indexes(self, first, second):
        def groups(manifest, directory):
            read = search.local_reader(*directory)
            return {g[2] + search.SUFFIX for g in search.read_index(read(search.root_of(manifest)))['groups']}
        return groups(second, [self.tmp / 'two', self.tmp / 'one']) - groups(first, [self.tmp / 'one'])

    def test_a_group_whose_changes_outgrow_the_limit_is_made_again(self):
        first, _ = self.build('one', self.places)
        base = search.read_base(search.local_reader(self.tmp / 'one'), first)
        now = json.loads(json.dumps(self.places))
        for r in now.values():
            r['rank'] = round(r['rank'] + 0.01, 2)
        with patch.object(search, 'CHANGES', 200):
            _, counts = self.build('two', now, base)
        self.assertEqual(counts['made'], first['datasets']['search']['groups'] - counts['kept'])
        self.assertGreater(counts['made'], 0)
        self.everything_is_found(self.tmp / 'two', now, [self.tmp / 'one'])



class AtOnceTests(unittest.TestCase):
    class Connection:
        def __init__(self, host):
            self.host = host
        def channel(self):
            if self.host['open'] == self.host['limit']:
                raise OSError('Administratively prohibited')
            self.host['open'] += 1
            return type(self)(self.host)
        def close_channel(self):
            self.host['closed'] += 1

    def test_channels_at_once_and_any_error_goes_on_one_by_one(self):
        host = {'open': 0, 'closed': 0, 'limit': 99}
        self.assertEqual(publish.at_once(self.Connection(host), range(20), lambda c, i: i * 2), [i * 2 for i in range(20)])
        self.assertEqual((host['open'], host['closed']), (publish.CHANNELS, publish.CHANNELS))
        host.update(open=0, closed=0, limit=2)  # a third refused: none used, all one by one
        main = self.Connection(host)
        self.assertEqual(publish.at_once(main, range(5), lambda c, i: (c is main, i)), [(True, i) for i in range(5)])
        self.assertEqual(host['closed'], 2)
        host.update(open=0, closed=0, limit=99)
        def flaky(c, i):  # a channel's error: what's left goes over the connection itself
            if c is not main and i == 3:
                raise OSError('channel closed')
            return i
        self.assertEqual(publish.at_once(main, range(10), flaky), list(range(10)))
        with self.assertRaises(OSError):
            publish.at_once(main, range(3), lambda c, i: (_ for _ in ()).throw(OSError('host down')))


class HostTests(unittest.TestCase):
    def test_upload_sends_only_what_the_host_lacks_and_promotion_points_at_it(self):
        host = RoutingHost()
        host.dirs.add(BASE)
        host.files[BASE + '/.htaccess'] = b'installed'
        places = {f'N{i}': rec(f'N{i}', f'Paikka{i}', 25, 61) for i in range(40)}
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', ENV | {'OSM_SHARED_FILES': 'true'}, clear=True), \
                patch.object(publish, 'open_sftp', host.ftp), patch.object(publish.urllib.request, 'urlopen', host.http), \
                patch.object(search.urllib.request, 'urlopen', host.http), patch.object(search, 'TARGET', 300):
            tmp = Path(tmp)
            first, _ = search.build(places, {}, tmp / 'one', None, T0)
            publish.upload(tmp / 'one', folder=search.FOLDER, budget=search.BUDGET)
            raw = host.files[BASE + '/candidates/current.json']
            sent = {p for p, _, _ in host.uploads if '/files/' in p or p.endswith('.gzt')}
            self.assertEqual(len([n for n in (tmp / 'one').iterdir() if n.suffix == '.gzt']), len(sent))
            url = search.folder_url()
            with self.assertRaises(publish.HTTPError):
                publish.candidate(url)  # another product's missing live pointer stops its check
            candidate = publish.candidate(url, first=True)
            self.assertEqual(candidate['sha256'], hashlib.sha256(raw).hexdigest(), 'nothing live yet: the first candidate')
            publish.promote_candidate(candidate['release'], candidate['sha256'], tmp / 'promote', search.FOLDER)
            self.assertEqual(json.loads(host.files[BASE + '/current.json']), publish.current(json.loads(raw)))
            # the next week: one place renamed; the host's release is the base
            host.uploads.clear()
            places['N7']['names'] = ['Uusi nimi']
            base = search.read_base(search.host_reader(url), search.newest(url))
            second, counts = search.build(places, {}, tmp / 'two', base, datetime(2026, 11, 30, tzinfo=timezone.utc))
            self.assertEqual((counts['made'], counts['new']), (0, 2), 'the new name\'s groups, uu and ni, get files')
            publish.upload(tmp / 'two', folder=search.FOLDER, budget=search.BUDGET)
            sent = [p for p, _, _ in host.uploads if p.endswith('.gzt')]
            self.assertEqual(len(sent), 5, "the root, pa's index, uu's and ni's indexes, and their one file: the same record")
            self.assertGreater(host.channels, 1, 'many small files go over several channels at once')
            candidate = publish.candidate(url)
            (tmp / 'promote2').mkdir()  # where the replaced pointer is kept for a rollback
            publish.promote_candidate(candidate['release'], candidate['sha256'], tmp / 'promote2', search.FOLDER)
            self.assertEqual(len(list((tmp / 'promote2').iterdir())), 1)
            self.assertEqual(json.loads(host.files[BASE + '/current.json'])['release'], second['release'])
            self.assertTrue(any(p.startswith(BASE + '/files/') for p in host.files))
            self.assertFalse(any(p.startswith(SITE + '/polku-osm/') for p in host.files), 'nothing in polku-osm')


if __name__ == '__main__':
    unittest.main()
