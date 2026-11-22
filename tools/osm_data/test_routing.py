"""The routing mirror against an in-memory host and brouter.de; BRouter's own check runs when BROUTER_JAR is set."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
import zlib

from tools.osm_data import publish, routing
from tools.osm_data.test_publish import ENV, Host, Response, ROOT

SITE = ROOT.removesuffix('/polku-osm')
BASE = SITE + '/polku-routing'
PUBLIC = 'https://data.example/polku-routing'
T0 = datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)
JAR, PATH = os.environ.get('BROUTER_JAR'), os.environ.get('PATH', '')


def square(seed, created, size=5000, version=11):
    """A square's layout: its index, BRouter's top checksum and creation time."""
    ends = [200 + size * (i + 1) // 25 for i in range(25)]
    header = struct.pack('>25q', *[(version if i == 0 else 2) << 48 | end for i, end in enumerate(ends)])
    top = ~zlib.crc32(header) & 0xffffffff ^ 2
    trailer = struct.pack('>qI', int(created.timestamp() * 1000), top) + bytes(100) + b'\x01'
    return header + bytes([seed]) * size + trailer


def md5(data):
    return hashlib.md5(data).hexdigest()


class RoutingHost(Host):
    def __init__(self):
        super().__init__()
        self.dirs.add(SITE)
        self.brouter, self.failing, self.requests = {}, {}, []

    def http(self, req, **kwargs):
        url = req.full_url
        if url.startswith(routing.SOURCE):
            return self.brouter_de(req)
        name = SITE + urlsplit(url).path
        if name not in self.files:
            raise HTTPError(url, 404, 'missing', {}, None)
        data, headers = self.files[name], dict(self.release_headers)
        headers['Content-Type'] = 'application/json' if name.endswith('.json') else 'application/octet-stream'
        if name.endswith('/current.json'):
            headers['Cache-Control'] = self.pointer_cache
        if byte_range := req.get_header('Range'):
            start, end = map(int, byte_range.removeprefix('bytes=').split('-'))
            headers |= {'Content-Range': f'bytes {start}-{end}/{len(data)}', 'Content-Length': str(end - start + 1)}
            return Response(data[start:end + 1], 206, headers)
        return Response(data, headers=headers | {'Content-Length': str(len(data))})

    def brouter_de(self, req):
        path = req.full_url.removeprefix(routing.SOURCE)
        self.requests.append((req.get_method(), path))
        if req.get_header('User-agent') != routing.AGENT:
            raise AssertionError('an honest User-Agent')
        if self.failing.get(path):
            self.failing[path] -= 1
            raise URLError('connection reset') if self.failing[path] else IncompleteRead(b'', 10)
        if path == '':
            rows = [f'<a href="{n}">{n}</a>      22-Nov-2026 01:03     {len(d)}' for n, d in sorted(self.brouter.items())
                    if '/' not in n]
            data = ('<html><pre><a href="diff/">diff/</a>      22-Nov-2026 03:14     -\n' + '\n'.join(rows) + '\n').encode()
        elif path in self.brouter:
            data = self.brouter[path]
        else:
            raise HTTPError(req.full_url, 404, 'missing', {}, None)
        # its index comes chunked, without a length, as nginx sends a directory listing
        return Response(b'' if req.get_method() == 'HEAD' else data, headers={} if path == '' else {'Content-Length': str(len(data))})

    def publish_square(self, name, data, previous=None):
        """brouter.de's newest copy, the change file from each previous copy, and the empty one for itself."""
        for path in [p for p in self.brouter if p.startswith(f'diff/{name[:-4]}/')]:
            del self.brouter[path]
        self.brouter[name] = data
        self.brouter[f'diff/{name[:-4]}/{md5(data)}.df5'] = b''
        for old in previous or []:
            self.brouter[f'diff/{name[:-4]}/{md5(old)}.df5'] = data


def fake_brouter(*args):
    """Stands in for Rd5.java: our change files carry the new square whole."""
    if args[0] == 'apply':
        old, change, new = args[1:]
        Path(new).write_bytes(Path(change).read_bytes() or Path(old).read_bytes())
    else:
        routing.describe(args[1])


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.host = RoutingHost()
        self.sleeps = []
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.dict('os.environ', ENV, clear=True))
        patches.enter_context(patch.object(publish, 'open_sftp', self.host.ftp))
        patches.enter_context(patch.object(routing.urllib.request, 'urlopen', self.host.http))
        patches.enter_context(patch.object(routing, 'SMALL', 3000))
        self.big, self.small = square(1, T0), square(2, T0, size=1000)
        self.host.publish_square('E20_N60.rd5', self.big)
        self.host.publish_square('E25_N70.rd5', self.small)
        self.south = square(3, T0)
        self.host.publish_square('E20_N55.rd5', self.south)
        self.host.files[BASE + '/.htaccess'] = b'installed by hand'

    def run_mirror(self, now=T0, initial=False):
        self.host.requests.clear()
        return routing.mirror(None, initial, now, routing.Source(self.sleeps.append), fake_brouter)

    def candidate(self):
        raw = self.host.files[BASE + '/candidates/current.json']
        manifest = json.loads(raw)
        self.assertEqual(self.host.files[f"{BASE}/releases/{manifest['release']}/manifest.json"], raw)
        return manifest

    def test_first_run_downloads_every_square_once_and_names_the_candidate(self):
        report = self.run_mirror(initial=True)
        self.assertEqual(report['initial'], ['E20_N55.rd5', 'E20_N60.rd5', 'E25_N70.rd5'])
        self.assertEqual(self.host.requests, [('GET', ''), ('GET', 'E20_N55.rd5'), ('HEAD', f'diff/E20_N55/{md5(self.south)}.df5'),
                                              ('GET', 'E20_N60.rd5'), ('HEAD', f'diff/E20_N60/{md5(self.big)}.df5'),
                                              ('GET', 'E25_N70.rd5')], 'one file at a time')
        manifest = self.candidate()
        squares = {s['path']: s for s in manifest['squares']}
        self.assertEqual(set(squares), {'E20_N55.rd5', 'E20_N60.rd5', 'E25_N70.rd5'})
        big = squares['E20_N60.rd5']
        self.assertEqual((big['bytes'], big['sha256'], big['md5']), (len(self.big), hashlib.sha256(self.big).hexdigest(), md5(self.big)))
        self.assertEqual(set(big), {'path', 'bytes', 'sha256', 'md5', 'created', 'downloaded', 'version', 'url'})
        self.assertEqual((big['created'], big['downloaded'], big['version']), ('2026-11-22T09:00:00Z', '2026-11-22T09:00:00Z', 11))
        self.assertEqual(big['url'], f"../../files/{big['sha256']}.rd5", 'relative to its release, among the shared files')
        self.assertIn('OpenStreetMap contributors', manifest['attribution'])
        self.assertIn('Hole-filled seamless SRTM data V4', manifest['attribution'], "CIAT's citation, as its terms ask")
        self.assertEqual(self.host.files[f"{BASE}/files/{big['sha256']}.rd5"], self.big)
        self.assertEqual(sorted(p.removeprefix(BASE) for p in self.host.files),
                         sorted(['/.htaccess', '/candidates/current.json', f"/releases/{manifest['release']}/manifest.json",
                                 *(f"/files/{s['sha256']}.rd5" for s in squares.values())]))
        self.assertNotIn(BASE + '/staging', self.host.dirs)
        self.assertEqual(routing.check(PUBLIC, T0 + timedelta(days=1)), [manifest['release']])

    def test_a_scheduled_run_never_downloads_the_world_again(self):
        with self.assertRaisesRegex(RuntimeError, 'the first download is the initial operation'):
            self.run_mirror()
        self.assertEqual(self.host.requests, [])

    def test_the_weekly_run_applies_change_files_and_keeps_the_rest(self):
        self.run_mirror(initial=True)
        first = self.candidate()['release']
        newer = square(1, T0 + timedelta(days=7), size=5100)
        self.host.publish_square('E20_N60.rd5', newer, previous=[self.big])
        self.host.publish_square('E25_N70.rd5', square(2, T0 + timedelta(days=7), size=1000))
        self.host.publish_square('E30_N60.rd5', square(4, T0))
        report = self.run_mirror(T0 + timedelta(days=7))
        self.assertEqual((report['diff'], report['unchanged'], report['kept'], report['waiting'], report['small']),
                         (1, 1, ['E25_N70.rd5'], ['E30_N60.rd5'], 0))
        self.assertEqual(self.host.requests, [('GET', ''), ('GET', f'diff/E20_N55/{md5(self.south)}.df5'),
                                              ('GET', f'diff/E20_N60/{md5(self.big)}.df5'),
                                              ('HEAD', f'diff/E20_N60/{md5(newer)}.df5')], 'no full download')
        manifest = self.candidate()
        squares = {s['path']: s for s in manifest['squares']}
        self.assertEqual(squares['E20_N60.rd5']['md5'], md5(newer))
        self.assertEqual(squares['E20_N60.rd5']['downloaded'], '2026-11-22T09:00:00Z', 'a change file is no full download')
        self.assertEqual(squares['E25_N70.rd5']['md5'], md5(self.small))
        self.assertNotIn(f'{BASE}/releases/{first}', self.host.dirs, 'only the live copy and one candidate stay')

    def shared_squares(self):
        return sorted(p.removeprefix(BASE + '/') for p in self.host.files if p.startswith(BASE + '/files/'))

    def test_a_square_that_didnt_change_is_not_sent_again(self):
        self.run_mirror(initial=True)
        newer = square(1, T0 + timedelta(days=7), size=5100)
        self.host.publish_square('E20_N60.rd5', newer, previous=[self.big])
        self.host.uploads.clear()
        report = self.run_mirror(T0 + timedelta(days=7))
        self.assertEqual((report['unchanged'], report['kept'], report['shared']), (1, ['E25_N70.rd5'], 2))
        self.assertEqual([name.removeprefix(BASE + '/') for name, _, _ in self.host.uploads if name.endswith('.rd5')],
                         ['staging/E20_N60.rd5'])
        manifest = self.candidate()
        self.assertEqual(self.shared_squares(), sorted(f"files/{s['sha256']}.rd5" for s in manifest['squares']),
                         "the old E20_N60 goes with the candidate that named it")
        self.assertEqual(routing.check(PUBLIC, T0 + timedelta(days=8)), [manifest['release']])

    def test_the_first_update_reads_a_candidate_from_before_the_shared_files(self):
        self.run_mirror(initial=True)
        old = self.candidate()
        for entry in old['squares']:  # as the mirror left them: in the release's own folder
            del entry['url']
            self.host.files[f"{BASE}/releases/{old['release']}/{entry['path']}"] = self.host.files.pop(
                f"{BASE}/files/{entry['sha256']}.rd5")
        raw = json.dumps(old, sort_keys=True, separators=(',', ':')).encode()
        self.host.files[f"{BASE}/releases/{old['release']}/manifest.json"] = self.host.files[BASE + '/candidates/current.json'] = raw
        routing.check(PUBLIC, T0 + timedelta(days=1))
        newer = square(1, T0 + timedelta(days=7), size=5100)
        self.host.publish_square('E20_N60.rd5', newer, previous=[self.big])
        report = self.run_mirror(T0 + timedelta(days=7))
        self.assertEqual((report['diff'], report['shared']), (1, 0), 'its squares go among the shared files, once')
        self.assertNotIn(f"{BASE}/releases/{old['release']}", self.host.dirs)
        manifest = self.candidate()
        self.assertEqual(self.shared_squares(), sorted(f"files/{s['sha256']}.rd5" for s in manifest['squares']))

    def test_a_square_without_its_change_file_waits_for_its_quarter(self):
        self.run_mirror(initial=True)
        newer = square(1, T0 + timedelta(days=20))
        self.host.publish_square('E20_N60.rd5', newer)
        report = self.run_mirror(T0 + timedelta(days=20))
        self.assertEqual((report['kept'], report['quarterly']), (['E20_N60.rd5', 'E25_N70.rd5'], []))
        report = self.run_mirror(T0 + timedelta(days=91))
        self.assertEqual((report['quarterly'], report['small']), (['E20_N60.rd5'], 1))
        squares = {s['path']: s for s in self.candidate()['squares']}
        self.assertEqual((squares['E20_N60.rd5']['md5'], squares['E20_N60.rd5']['downloaded']), (md5(newer), '2027-02-21T09:00:00Z'))

    def test_a_stopped_run_resumes_without_downloading_again(self):
        self.host.failing['E25_N70.rd5'] = 2
        with self.assertRaisesRegex(RuntimeError, 'failed twice on E25_N70.rd5'):
            self.run_mirror(initial=True)
        self.assertEqual([s for s in self.sleeps if s >= 1], [routing.BACK_OFF], 'no quick retries')
        self.assertIn(BASE + '/staging/_E20_N60.rd5.json', self.host.files)
        report = self.run_mirror(T0 + timedelta(hours=1), initial=True)
        self.assertEqual(report['initial'], ['E25_N70.rd5'])
        self.assertNotIn(('GET', 'E20_N60.rd5'), self.host.requests)
        self.assertEqual(len(self.candidate()['squares']), 3)

    def test_a_new_lookup_version_or_a_copy_brouter_doesnt_know_stops(self):
        self.run_mirror(initial=True)
        self.host.publish_square('E20_N60.rd5', square(1, T0 + timedelta(days=7), version=12), previous=[self.big])
        with self.assertRaisesRegex(RuntimeError, 'lookup version changed'):
            self.run_mirror(T0 + timedelta(days=7))
        newer = square(1, T0 + timedelta(days=7), size=5100)
        self.host.publish_square('E20_N60.rd5', newer, previous=[self.big])
        del self.host.brouter[f'diff/E20_N60/{md5(newer)}.df5']
        with self.assertRaisesRegex(RuntimeError, 'no change file for our new E20_N60.rd5'):
            self.run_mirror(T0 + timedelta(days=7))

    def test_the_daily_check_needs_fresh_squares_and_public_ranges(self):
        self.run_mirror(initial=True)
        with self.assertRaisesRegex(ValueError, 'E20_N55.rd5 is 15 days old'):
            routing.check(PUBLIC, T0 + timedelta(days=15))
        self.assertTrue(routing.check(PUBLIC, T0 + timedelta(days=13)))
        self.host.release_headers['Access-Control-Allow-Origin'] = 'https://other.example'
        with self.assertRaisesRegex(ValueError, 'wildcard CORS'):
            routing.check(PUBLIC, T0 + timedelta(days=1))
        self.host.release_headers['Access-Control-Allow-Origin'] = '*'
        path = f'{BASE}/files/{hashlib.sha256(self.big).hexdigest()}.rd5'
        self.host.files[path] = bytes(200) + self.host.files[path][200:]
        with self.assertRaisesRegex(ValueError, 'no valid index: E20_N60.rd5'):
            routing.check(PUBLIC, T0 + timedelta(days=1))

    def test_a_manifest_needs_each_squares_size_hashes_and_version(self):
        self.run_mirror(initial=True)
        manifest = self.candidate()
        routing.validate(manifest)
        for change in [{'bytes': 311}, {'version': '11'}, {'md5': 'x'}, {'path': '../E20_N60.rd5'}]:
            with self.subTest(change):
                broken = json.loads(json.dumps(manifest))
                broken['squares'][0].update(change)
                with self.assertRaises(ValueError):
                    routing.validate(broken)

    def test_hosting_is_installed_once_and_never_replaced(self):
        del self.host.files[BASE + '/.htaccess']
        with self.assertRaisesRegex(RuntimeError, 'run install-hosting first'):
            self.run_mirror(initial=True)
        self.assertEqual(self.host.requests, [])
        routing.install_hosting()
        self.assertEqual(self.host.files[BASE + '/.htaccess'], Path(routing.__file__).with_name('routing.htaccess').read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'change it by hand'):
            routing.install_hosting()

    def test_downloads_stay_under_16_mbit(self):
        clock = [0.0]
        source = routing.Source(lambda seconds: clock.__setitem__(0, clock[0] + seconds), lambda: clock[0])
        self.host.brouter['E0_N0.rd5'] = bytes(routing.RATE * 3000)
        with tempfile.TemporaryDirectory() as tmp:
            source.get('E0_N0.rd5', Path(tmp) / 'square')
        self.assertAlmostEqual(clock[0], 3.0, places=2)

    @unittest.skipUnless(JAR, "BRouter's release jar")
    def test_brouters_own_check_fails_a_damaged_square(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with patch.dict('os.environ', {'PATH': PATH}):
                brouter = routing.BRouter(JAR, tmp)
            empty = square(0, T0, size=0)
            (tmp / 'good.rd5').write_bytes(empty)
            brouter('check', tmp / 'good.rd5')
            (tmp / 'none.df5').write_bytes(b'')
            brouter('apply', tmp / 'good.rd5', tmp / 'none.df5', tmp / 'same.rd5')
            self.assertEqual((tmp / 'same.rd5').read_bytes(), empty)
            (tmp / 'bad.rd5').write_bytes(empty[:100] + b'\x01' + empty[101:])
            with self.assertRaises(subprocess.CalledProcessError):
                brouter('check', tmp / 'bad.rd5')


if __name__ == '__main__':
    unittest.main()
