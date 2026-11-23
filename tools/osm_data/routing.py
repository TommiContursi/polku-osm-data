#!/usr/bin/env python3
"""A mirror of BRouter's routing data (brouter.de's segments4) on Polku's host, uploaded as a candidate Polku promotes.

brouter.de's terms (abrensch/brouter#1011, 2026-11-12): one initial download, then regular syncs by its change files,
as BRouter's Android app does. So each square is our last copy plus brouter.de's .df5 named by that copy's MD5,
applied and checked by BRouter's own code (Rd5.java), one file at a time and at most 16 Mbit/s (#7). Each changed square
also has the mirror's change file from the previous release's square: brouter.de's own, or one made and checked by
applying it with the same code."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.error import HTTPError

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.osm_data import maintenance, publish
from tools.osm_data.publish import require

SOURCE = 'https://brouter.de/brouter/segments4/'
AGENT = 'Polku-routing-mirror/1.0 (+https://github.com/TommiContursi/polku-osm-data)'
RATE = 2096  # bytes per millisecond: BRouter's app keeps each download under 16 Mbit/s
BACK_OFF = 300  # seconds before the one retry; a second failure stops the run, and the next run resumes
SQUARE = re.compile(r'([EW])(\d{1,3})_([NS])(\d{1,2})\.rd5\Z')
STAGED = re.compile(r'[EW]\d{1,3}_[NS]\d{1,2}\.(rd5|df5)\Z')  # a square or its change file, as staging names them
SMALL = 1024 * 1024  # BRouter makes no change files for squares under 1 MiB (Rd5DiffManager)
QUARTER = timedelta(days=90)  # brouter.de allows a square's full download about once a quarter
FRESH, OLDEST = timedelta(days=14), timedelta(days=100)  # squares with change files, and the small ones
BUDGET = 32 * 1024**3  # live, candidate and the staging one between them: three copies of the world's 10.1 GB
STAGE = 'staging'
ATTRIBUTION = ('Routing data © OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright), '
               'built by BRouter (https://brouter.de). Elevation north of 60°N: Sonny\'s LiDAR digital terrain models '
               '(https://sonny.4lima.de), CC BY 4.0. Elevation south of 60°N: Jarvis A., H.I. Reuter, A. Nelson, '
               'E. Guevara, 2008, Hole-filled seamless SRTM data V4, International Centre for Tropical Agriculture '
               '(CIAT), https://srtm.csi.cgiar.org.')


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def when(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def listing(page):
    """brouter.de's squares and their sizes, from its index page."""
    rows = dict((name, int(size)) for name, size in re.findall(r'^<a href="([^"/]+\.rd5)">.*\s(\d+)\s*$', page, re.M))
    require(rows and all(SQUARE.fullmatch(name) for name in rows), "brouter.de's index changed")
    return rows


def describe(path):
    """BRouter's lookup version and creation time, from the square's index and the checksums after it."""
    with open(path, 'rb') as square:
        longs = struct.unpack('>25q', square.read(200))
        square.seek(longs[24] & 0xffffffffffff)
        trailer = square.read(112)
    require(len(trailer) == 112, 'Square without its checksums')
    return {'version': longs[0] >> 48,
            'created': stamp(datetime.fromtimestamp(struct.unpack('>q', trailer[:8])[0] / 1000, timezone.utc))}


def record(path, name, downloaded):
    sha256, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, 'rb') as square:
        while chunk := square.read(1024 * 1024):
            sha256.update(chunk)
            md5.update(chunk)
    return {'path': name, 'bytes': path.stat().st_size, 'sha256': sha256.hexdigest(), 'md5': md5.hexdigest(),
            'downloaded': downloaded} | describe(path)


def validate(manifest):
    require(manifest.get('schema') == 1 and publish.NAME.fullmatch(manifest.get('release', ''))
            and manifest.get('attribution') and isinstance(manifest.get('squares'), list) and manifest['squares'],
            'Invalid routing manifest')
    names = [square.get('path') for square in manifest['squares']]
    require(len(set(names)) == len(names) and all(isinstance(n, str) and SQUARE.fullmatch(n) for n in names),
            'Unsafe or duplicate routing square')
    for square in manifest['squares']:
        require(type(square.get('bytes')) is int and square['bytes'] >= 312 and type(square.get('version')) is int
                and re.fullmatch('[a-f0-9]{64}', square.get('sha256', '')) and re.fullmatch('[a-f0-9]{32}', square.get('md5', '')),
                'Invalid routing square ' + square['path'])
        when(square['created']), when(square['downloaded'])
        publish.place(manifest['release'], square)  # checks its url
        if 'change' in square:
            change = square['change']
            require(change.get('path') == square['path'][:-4] + '.df5' and 'url' in change and type(change.get('bytes')) is int
                    and change['bytes'] >= 200 and re.fullmatch('[a-f0-9]{64}', change.get('sha256', ''))
                    and re.fullmatch('[a-f0-9]{64}', change.get('from', '')), 'Invalid change file for ' + square['path'])
            publish.place(manifest['release'], change)
    return manifest



class Source:
    """brouter.de, politely: one request at a time, each file paced, and a failure other than 404 waits once, then
    stops the run."""
    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self.sleep, self.clock = sleep, clock
        self.requests = self.bytes = 0

    def get(self, path, target=None):
        """Into target, or only whether it exists without one (HEAD); None when brouter.de has no such file."""
        for attempt in (1, 2):
            try:
                return self.fetch(path, target)
            except HTTPError as error:
                error.close()
                if error.code == 404:
                    return None
                failure = error
            except (OSError, ValueError, http.client.HTTPException) as error:  # a body cut short too
                failure = error
            if attempt == 1:
                print(f'brouter.de {path}: {failure}; one retry in {BACK_OFF} s', flush=True)
                self.sleep(BACK_OFF)
        raise RuntimeError(f'brouter.de failed twice on {path} ({failure}); stopped, and the next run resumes')

    def fetch(self, path, target):
        request = urllib.request.Request(SOURCE + path, method='GET' if target else 'HEAD', headers={'User-Agent': AGENT})
        self.requests += 1
        with urllib.request.urlopen(request, timeout=60) as response:
            if target is None:
                return True
            length, seen, start = int(response.headers.get('Content-Length', -1)), 0, self.clock()
            with open(target, 'wb') as body:
                while chunk := response.read(256 * 1024):
                    body.write(chunk)
                    seen += len(chunk)
                    if (ahead := seen / RATE / 1000 - (self.clock() - start)) > 0:
                        self.sleep(ahead)
            # brouter.de sends its index without a length; squares and change files have one
            require(length < 0 or seen == length, f'Short download: {seen} of {length} bytes')
            self.bytes += seen
            return target


class BRouter:
    """BRouter 1.7.10's own code, from its release jar, without the job's credentials in its environment."""
    def __init__(self, jar, work):
        classes = work / 'classes'
        self.env = {'PATH': os.environ.get('PATH', '')}
        subprocess.run(['javac', '-cp', str(jar), '-d', str(classes), str(Path(__file__).with_name('Rd5.java'))],
                       check=True, env=self.env)
        self.classpath = f'{jar}{os.pathsep}{classes}'

    def __call__(self, *args):
        subprocess.run(['java', '-cp', self.classpath, 'Rd5', *map(str, args)], check=True, env=self.env,
                       stdout=subprocess.DEVNULL)


@contextmanager
def routing_host():
    """The pinned-key SFTP login, in polku-routing beside polku-osm, and the folder's public URL."""
    config = publish.settings()
    require(config[4].endswith('/polku-osm'), 'OSM_PUBLIC_URL must end in /polku-osm')
    with publish.connection(config) as ftp:
        ftp.cwd('..')
        if 'polku-routing' not in maintenance.children(ftp):
            ftp.mkd('polku-routing')
        require(ftp.kind('polku-routing') == 'directory', 'polku-routing needs a real directory')
        ftp.cwd('polku-routing')
        yield ftp, config[4].removesuffix('/polku-osm') + '/polku-routing'


def pointed(ftp, name):
    """The manifest a pointer names, or None when there is no pointer."""
    folder, _, base = name.rpartition('/')
    if folder and folder not in maintenance.children(ftp) or base not in maintenance.children(ftp, folder or '.'):
        return None
    raw = publish.retrieve(ftp, name)
    manifest = validate(json.loads(raw))
    require(publish.retrieve(ftp, f"releases/{manifest['release']}/manifest.json") == raw, name + ' differs from its release')
    return manifest


def remove_tree(ftp, base):
    """Only what this mirror writes: squares, change files, their records and manifests."""
    for name in maintenance.children(ftp, base):
        require(ftp.kind(base + '/' + name) == 'file' and (STAGED.fullmatch(name) or name == 'manifest.json'
                or re.fullmatch(r'_[A-Za-z0-9_.]+\.json', name)), f'Unexpected file {base}/{name}; inspect by hand')
        ftp.remove(base + '/' + name)
    ftp.rmd(base)


def staging(ftp, base, now):
    """Resume the staging of the same base, or start one. _run.json names its release and base, and each square done
    has its record here once its file, and its change file, are among the shared files."""
    if STAGE in maintenance.children(ftp):
        require(ftp.kind(STAGE) == 'directory', 'staging needs a real directory')
        names = maintenance.children(ftp, STAGE)
        run = json.loads(publish.retrieve(ftp, STAGE + '/_run.json')) if '_run.json' in names else {}
        if run and run.get('base') == base:
            done, have = {}, maintenance.shared_files(ftp)
            for name in names:
                square = name[1:-5]
                if name.startswith('_') and SQUARE.fullmatch(square):
                    entry = json.loads(publish.retrieve(ftp, f'{STAGE}/{name}'))
                    if all(have.get(publish.place(run['release'], f)) == f['bytes'] for f in (entry, entry.get('change')) if f):
                        done[square] = entry
            print(f"Resume {run['release']} with {len(done)} verified squares", flush=True)
            return run['release'], done
        remove_tree(ftp, STAGE)
    release = now.strftime('%Y%m%dT%H%M%SZ')
    ftp.mkd(STAGE)
    ftp.storbinary(f'STOR {STAGE}/_run.json', io.BytesIO(json.dumps({'release': release, 'base': base}).encode()))
    return release, {}


def put(ftp, release, local, entry, have):
    """Into the shared files, unless they hold it: uploaded, read back against its record, then moved into place.
    Whether it was sent."""
    target = publish.place(release, entry)
    if target in have:
        require(have[target] == entry['bytes'], 'Shared file differs: ' + target)
        return False
    path = f"{STAGE}/{entry['path']}"
    with open(local, 'rb') as body:
        ftp.storbinary('STOR ' + path, body)
    sha256 = hashlib.sha256()
    ftp.retrbinary('RETR ' + path, sha256.update)
    require(ftp.size(path) == entry['bytes'] and sha256.hexdigest() == entry['sha256'], 'Uploaded file differs: ' + path)
    publish.move_shared(ftp, path, target)
    have[target] = entry['bytes']
    return True


def probe(url, manifest, raw):
    """Public: the immutable manifest, and every square's index by Range, with CORS and without compression. A change
    file starts with the index of the square it makes."""
    require(publish.get(f"{url}/releases/{manifest['release']}/manifest.json", immutable=True) == raw,
            'Public routing manifest differs')
    for square in manifest['squares']:
        head = publish.get(f"{url}/{publish.place(manifest['release'], square)}", (0, 200, square['bytes']), True)
        ends = [value & 0xffffffffffff for value in struct.unpack('>25q', head)]
        require(200 <= ends[0] and ends == sorted(ends) and ends[24] + 112 <= square['bytes'],
                'Public square has no valid index: ' + square['path'])
        if change := square.get('change'):
            require(publish.get(f"{url}/{publish.place(manifest['release'], change)}", (0, 200, change['bytes']), True) == head,
                    'Public change file differs from its square: ' + square['path'])


def mirror(jar, initial=False, now=None, source=None, brouter=None):
    """Update every square from our last candidate by brouter.de's change files, or download it whole when it has none
    and its quarter is up (a new square only with initial), and upload the result as the new candidate."""
    now, source = now or datetime.now(timezone.utc), source or Source()
    require(os.environ.get('OSM_ALLOW_PUBLISH') == 'true', 'Publication requires approved OSM_ALLOW_PUBLISH=true')
    report = {'initial': [], 'diff': 0, 'unchanged': 0, 'quarterly': [], 'small': 0, 'kept': [], 'waiting': [], 'shared': 0,
              'changes': 0}
    with tempfile.TemporaryDirectory() as tmp, routing_host() as (ftp, url):
        work = Path(tmp)
        require('.htaccess' in maintenance.children(ftp), 'polku-routing has no .htaccess: run install-hosting first')
        base = pointed(ftp, 'candidates/current.json')
        require(base or initial, 'No candidate to update: the first download is the initial operation')
        brouter = brouter or BRouter(jar, work)
        page = source.get('', work / 'index.html')
        require(page, "brouter.de's index is missing")
        listed = listing(page.read_text())
        names = sorted(listed)
        previous = {square['path']: square for square in base['squares']} if base else {}
        release, done = staging(ftp, base and base['release'], now)
        have = maintenance.shared_files(ftp)
        incoming = sum(listed[name] for name in names if name not in done)
        print(json.dumps({'release': release, 'squares': len(names), 'to_do': len(names) - len(done),
                          'incoming_bytes': incoming}), flush=True)
        require(maintenance.usage(ftp) + incoming <= BUDGET, 'Routing storage budget exceeded (32 GiB)')
        for name in names:
            if name in done:
                continue
            old, new = previous.get(name), work / name
            if old is None:
                if not initial:
                    report['waiting'].append(name)
                    continue
                require(source.get(name, new), f'brouter.de lists {name} but has no file')
                kind, downloaded = 'initial', stamp(now)
            else:
                copy = work / ('base-' + name)
                with open(copy, 'wb') as body:
                    ftp.retrbinary(f"RETR {publish.place(base['release'], old)}", body.write)
                copy_entry = record(copy, name, old['downloaded'])
                require(copy_entry['sha256'] == old['sha256'], 'Our copy differs: ' + name)
                change = old['bytes'] >= SMALL and source.get(f"diff/{name[:-4]}/{old['md5']}.df5", work / 'change.df5')
                if change:
                    brouter('apply', copy, change, new)
                    kind, downloaded = 'diff' if change.stat().st_size else 'unchanged', old['downloaded']
                elif now - when(old['downloaded']) >= QUARTER:
                    require(source.get(name, new), f'brouter.de no longer has {name}')
                    kind, downloaded = 'quarterly' if old['bytes'] >= SMALL else 'small', stamp(now)
                else:
                    kind, new = 'kept', copy
            if kind != 'kept':
                brouter('check', new)
            entry = publish.share(copy_entry if kind == 'kept' else record(new, name, downloaded))
            require(not old or entry['version'] == old['version'],
                    f"brouter.de's lookup version changed on {name}; stopped before publishing it")
            if kind not in {'kept', 'unchanged'} and entry['bytes'] >= SMALL:
                # Next week's change file is named by this MD5: brouter.de names its newest copy with an empty one
                require(source.get(f"diff/{name[:-4]}/{entry['md5']}.df5"), f'brouter.de has no change file for our new {name}')
                if old:  # ours from the previous release's square, for squares of 1 MiB or more as brouter.de's
                    if kind != 'diff':
                        change = work / 'change.df5'
                        brouter('diff', copy, new, change)
                        brouter('apply', copy, change, work / 'applied.rd5')
                        require(publish.digest_file(work / 'applied.rd5') == entry['sha256'], 'Our change file fails: ' + name)
                    entry['change'] = publish.share({'path': name[:-4] + '.df5', 'bytes': change.stat().st_size,
                                                     'sha256': publish.digest_file(change), 'from': old['sha256']})
            if not put(ftp, release, new, entry, have):
                report['shared'] += 1
            if 'change' in entry:
                put(ftp, release, change, entry['change'], have)
                report['changes'] += 1
            ftp.storbinary(f'STOR {STAGE}/_{name}.json', io.BytesIO(json.dumps(entry).encode()))
            done[name] = entry
            if isinstance(report[kind], list):
                report[kind].append(name)
            else:
                report[kind] += 1
            for path in work.glob('*.rd5'):
                path.unlink()
        squares = [done[name] for name in names if name in done]
        require(len({square['version'] for square in squares}) == 1, 'Squares of different lookup versions')
        manifest = validate({'schema': 1, 'release': release, 'built_at': stamp(datetime.now(timezone.utc)),
                             'source': SOURCE, 'engine': 'BRouter 1.7.10', 'attribution': ATTRIBUTION, 'squares': squares})
        raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
        ftp.storbinary(f'STOR {STAGE}/manifest.json', io.BytesIO(raw))
        require(publish.retrieve(ftp, f'{STAGE}/manifest.json') == raw, 'Uploaded routing manifest differs')
        # The records go before _run.json: a run stopped here resumes, or starts again, from what is left
        for name in maintenance.children(ftp, STAGE):  # records, and any file a stopped upload left
            if name not in {'_run.json', 'manifest.json'} and (name.startswith('_') or STAGED.fullmatch(name)):
                ftp.remove(f'{STAGE}/{name}')
        ftp.remove(f'{STAGE}/_run.json')
        require(maintenance.children(ftp, STAGE) == ['manifest.json'], 'Unexpected files in staging')
        if 'releases' not in maintenance.children(ftp):
            ftp.mkd('releases')
        ftp.rename(STAGE, f'releases/{release}')
        probe(url, manifest, raw)
        if 'candidates' not in maintenance.children(ftp):
            ftp.mkd('candidates')
        publish.replace_pointer(ftp, raw, 'candidates/current.json')
        require(publish.get(url + '/candidates/current.json') == raw, 'Public candidate pointer differs')
        live = pointed(ftp, 'current.json')
        for folder in maintenance.children(ftp, 'releases'):
            if folder not in {release, live and live['release']}:
                remove_tree(ftp, 'releases/' + folder)
        # then the shared squares and change files neither of them names
        named = {publish.place(m['release'], f) for m in (manifest, live) if m for square in m['squares']
                 for f in (square, square.get('change')) if f and 'url' in f}
        for path in sorted(maintenance.shared_files(ftp).keys() - named):
            ftp.remove(path)
    report |= {'release': release, 'dropped': sorted(set(previous) - set(names)), 'squares': len(squares), 'bytes': sum(square['bytes'] for square in squares),
               'brouter_requests': source.requests, 'brouter_bytes': source.bytes}
    summary = json.dumps(report, sort_keys=True)
    print(summary, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out:
            out.write(f'### Routing data\n\n```json\n{summary}\n```\n')
    print(f'Candidate {release}: {url}/releases/{release}/manifest.json', flush=True)
    return report


def check(url, now=None):
    """Daily, public HTTP only: the candidate and, once Polku promotes one, the live release."""
    url, now = publish.public_url(url), now or datetime.now(timezone.utc)
    checked = []
    for name in ('current.json', 'candidates/current.json'):
        try:
            raw = publish.get(f'{url}/{name}')
        except HTTPError as error:
            error.close()
            if error.code == 404 and name == 'current.json':
                continue  # not promoted yet
            raise
        manifest = validate(json.loads(raw))
        require(publish.get(f"{url}/releases/{manifest['release']}/manifest.json", immutable=True) == raw,
                name + ' differs from its release')
        for square in manifest['squares']:
            age = now - when(square['created'])
            require(age <= (FRESH if square['bytes'] >= SMALL else OLDEST), f"{name}: {square['path']} is {age.days} days old")
        probe(url, manifest, raw)
        checked.append(manifest['release'])
    return checked


def install_hosting():
    """The routing folder's .htaccess, once: an existing one is changed by hand."""
    with routing_host() as (ftp, _):
        require('.htaccess' not in maintenance.children(ftp), 'polku-routing/.htaccess exists; change it by hand')
        ftp.storbinary('STOR .htaccess', io.BytesIO(Path(__file__).with_name('routing.htaccess').read_bytes()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('mirror')
    run.add_argument('--jar', type=Path, required=True, help="BRouter 1.7.10's brouter-1.7.10-all.jar")
    run.add_argument('--initial', action='store_true', help='download squares we have no copy of: their one initial download')
    commands.add_parser('check').add_argument('url', help='the public polku-routing URL')
    commands.add_parser('install-hosting')
    args = parser.parse_args()
    try:
        if args.command == 'mirror':
            mirror(args.jar, args.initial)
        elif args.command == 'check':
            print(json.dumps(check(args.url)))
        else:
            install_hosting()
    except Exception as error:
        parser.exit(1, f'{args.command} failed: {error}\n')
    print(f'{args.command}: verified')


if __name__ == '__main__':
    main()
