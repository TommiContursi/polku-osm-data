#!/usr/bin/env python3
"""A copy of the National Land Survey of Finland's (MML) background-map vector tiles, taustakartta v21, zooms 11-14
over a box, fetched once at a paced rate, packed into one PMTiles archive and uploaded as a candidate in polku-mml.
Polku's mml-promote.yml checks the candidate in the app and promotes it: polku-mml/current.json, the manifest's own
bytes, is the release people get.

The tiles are CC BY 4.0. MML may limit a user whose volume disturbs its service, so the fetch halves its rate at any
sign of load and stops when MML refuses the key. Each tile's Last-Modified goes with the release, so a later update
can ask again with If-Modified-Since. The key (MML_API_KEY) is only ever put into requests: never printed or stored."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pmtiles.reader import MmapSource, all_tiles
from pmtiles.tile import Compression, TileType, tileid_to_zxy, zxy_to_tileid
from pmtiles.writer import Writer
from tools.osm_data import maintenance, publish
from tools.osm_data.publish import require
from tools.osm_data.routing import stamp

HOST = 'avoin-karttakuva.maanmittauslaitos.fi'
PATH = '/vectortiles/taustakartta/wmts/1.0.0/taustakartta/default/v21/WGS84_Pseudo-Mercator/{z}/{y}/{x}.pbf'
SOURCE = f'https://{HOST}{PATH}'
AGENT = 'polku-osm-data/1.0 (+https://github.com/TommiContursi/polku-osm-data)'
BOX = (19.0, 59.3, 31.6, 70.1)  # west, south, east, north
ZOOMS = range(11, 15)
HELSINKI = (24.9454, 60.1699)  # the hand check's place: its tiles name it
RATE, FLOOR = 40, 1  # requests a second over all connections; halved on trouble, down to the floor
CONNECTIONS = 8  # keep-alive; MML answers from Finland, so 40 a second needs a few in flight
TIMEOUT, SLOW = 10, 2  # seconds; an answer slower than SLOW counts as trouble
CALM, RAISE = 30, 1  # after CALM seconds without trouble the rate rises by RAISE, up to RATE
GIVE_UP = 300  # seconds without one good answer, or of 429s at the floor rate, before the run stops
ATTEMPTS = 3  # per tile in one run; a tile still unanswered waits for the next run
EMPTY = gzip.compress(b'', 6, mtime=0)  # MML's 404: an empty tile
ARCHIVE, TABLE, LICENSE = 'taustakartta.pmtiles', 'tiles.tsv.gz', 'LICENSE.txt'
FOLDER, STAGE = 'polku-mml', 'staging'
BUDGET = 30 * 1024**3  # live, candidate and staging: three copies of about 9 GB


class Refused(Exception):
    """MML refused the key or kept limiting us: stop the whole run and ask the user."""


def column(lon, z):
    return int((lon + 180) / 360 * 2 ** z)


def row(lat, z):
    r = math.radians(lat)
    return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * 2 ** z)


def tile_ids(box):
    """Every tile touching the box at each zoom, as sorted PMTiles tile IDs (z, x, y; MML's URLs are z/y/x)."""
    west, south, east, north = box
    return sorted(zxy_to_tileid(z, x, y) for z in ZOOMS for x in range(column(west, z), column(east, z) + 1)
                  for y in range(row(north, z), row(south, z) + 1))


def varint(data, i):
    value = shift = 0
    while True:
        require(i < len(data) and shift < 64, 'Vector tile cut short')
        value, shift, i = value | (data[i] & 0x7f) << shift, shift + 7, i + 1
        if data[i - 1] < 0x80:
            return value, i


def fields(data):
    """A protobuf message's (field number, value) pairs."""
    i = 0
    while i < len(data):
        key, i = varint(data, i)
        if key & 7 == 0:
            value, i = varint(data, i)
        else:
            require(key & 7 in (1, 2, 5), 'Not a vector tile')
            size, i = (8, i) if key & 7 == 1 else (4, i) if key & 7 == 5 else varint(data, i)
            value, i = data[i:i + size], i + size
            require(len(value) == size, 'Vector tile cut short')
        yield key >> 3, value


def layers(tile):
    """A vector tile's layer names; ValueError for anything else, such as an error page or a gzipped body."""
    names = []
    for number, layer in fields(tile):
        require(number == 3 and isinstance(layer, bytes), 'Not a vector tile')
        name = [value for n, value in fields(layer) if n == 1]
        require(len(name) == 1 and isinstance(name[0], bytes), 'Vector tile layer without its name')
        names.append(name[0].decode())
    return names


class MML:
    """One GET per tile, on this thread's keep-alive connection. Returns (status, headers, body, seconds until the
    answer began): MML's own pace, apart from the time a large tile takes to cross."""
    def __init__(self, key):
        require(re.fullmatch(r'[A-Za-z0-9-]{8,128}', key or ''), 'MML_API_KEY is missing or malformed')
        self.query, self.local = '?' + urlencode({'api-key': key}), threading.local()

    def __call__(self, z, x, y):
        connection = getattr(self.local, 'connection', None)
        if connection is None:
            connection = self.local.connection = http.client.HTTPSConnection(HOST, timeout=TIMEOUT)
        start = time.monotonic()
        try:
            connection.request('GET', PATH.format(z=z, x=x, y=y) + self.query,
                               headers={'User-Agent': AGENT, 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            seconds = time.monotonic() - start
            body = response.read()
        except BaseException:
            connection.close()
            self.local.connection = None
            raise
        return response.status, response.headers, body, seconds


class Pace:
    """Requests spread evenly over all connections at `rate` a second: halved at most every 5 s on trouble, down to
    FLOOR, and raised by RAISE after every CALM seconds without it."""
    def __init__(self, rate, clock=time.monotonic, sleep=time.sleep):
        self.top = self.rate = rate
        self.clock, self.sleep, self.lock = clock, sleep, threading.Lock()
        self.next = self.calm = clock()
        self.halved, self.floor_since = -math.inf, None

    def wait(self):
        with self.lock:
            now = self.clock()
            if now - self.calm >= CALM and self.rate < self.top:
                self.rate, self.calm, self.floor_since = min(self.top, self.rate + RAISE), now, None
            slot = self.next = max(now, self.next)
            self.next += 1 / self.rate
        if slot > now:
            self.sleep(slot - now)

    def trouble(self, pause=0):
        with self.lock:
            now = self.clock()
            self.calm, self.next = now, max(self.next, now + pause)
            if now - self.halved >= 5:
                self.rate, self.halved = max(FLOOR, self.rate / 2), now
                if self.rate == FLOOR and self.floor_since is None:
                    self.floor_since = now


def answer(status, headers, body):
    """What a response means: ('tile', gzipped body), ('empty', None), ('recorded', None) for another 4xx,
    ('refused', reason) or ('trouble', reason)."""
    if status == 200:
        length = headers.get('Content-Length')
        if headers.get('Content-Encoding', 'identity') != 'identity' or body[:2] == b'\x1f\x8b':
            return 'trouble', 'compressed body'
        if length is not None and length != str(len(body)):
            return 'trouble', 'short body'
        try:
            layers(body)
        except ValueError as error:
            return 'trouble', str(error)
        return 'tile', gzip.compress(body, 6, mtime=0)
    if status == 404:
        return 'empty', None
    if status in (401, 403):
        return 'refused', f'HTTP {status}'
    if 400 <= status < 500 and status != 429:
        return 'recorded', None
    return 'trouble', f'HTTP {status}'


def store(path, box=None):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS tiles (id INTEGER PRIMARY KEY, status INTEGER NOT NULL, modified TEXT, data BLOB)')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    meta = dict(db.execute('SELECT key, value FROM meta'))
    if box is not None:
        if 'box' not in meta:
            db.execute("INSERT INTO meta VALUES ('box', ?)", (json.dumps(list(box)),))
            db.commit()
        require(json.loads(meta.get('box', json.dumps(list(box)))) == list(box), f"The store is for the box {meta.get('box')}")
    return db


def fetch(path, box=BOX, until=math.inf, limit=None, request=None, rate=RATE, connections=CONNECTIONS):
    """Every tile of the box the store doesn't hold yet, until the deadline (epoch seconds) or `limit` tiles. Raises
    Refused when MML refuses the key or keeps limiting us; what was fetched is kept either way."""
    db, every = store(path, box), tile_ids(box)
    held = {tile for (tile,) in db.execute('SELECT id FROM tiles')}
    todo, total = [tile for tile in every if tile not in held][:limit], len(every)
    request = request or MML(os.environ.get('MML_API_KEY'))
    print(json.dumps({'held': len(held), 'to_fetch': len(todo), 'rate': rate, 'connections': connections}), flush=True)
    if todo:
        db.execute("INSERT OR IGNORE INTO meta VALUES ('fetched_from', ?)", (stamp(datetime.now(timezone.utc)),))
    pace, work, done, stop = Pace(rate), queue.Queue(), queue.Queue(), threading.Event()

    def worker():
        while (item := work.get()) is not None:
            if not (stop.is_set() or time.time() >= until):
                pace.wait()  # a stopped run hands back what is left at once, unpaced
            if stop.is_set() or time.time() >= until:
                done.put((item, None, None))
                continue
            try:
                done.put((item, request(*tileid_to_zxy(item[0])), None))
            except Exception as error:  # the class only: a message could hold the URL
                done.put((item, None, type(error).__name__))

    for tile in todo:
        work.put((tile, 1))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(connections)]
    for thread in threads:
        thread.start()
    report, seen, outstanding = Counter(), Counter(), len(todo)
    began = last_ok = last_print = last_commit = time.monotonic()
    answered = printed = 0
    failure = None
    try:
        while outstanding:
            (tile, attempt), result, error = done.get()
            outstanding -= 1
            now = time.monotonic()
            if result is None and error is None:
                report['left'] += 1  # stopped before asking
                continue
            answered += 1
            if result is None:
                kind, detail, pause = 'trouble', error, 0
            else:
                status, headers, body, seconds = result
                kind, detail = answer(status, headers, body)
                pause = int(headers.get('Retry-After', '0')) if status == 429 and headers.get('Retry-After', '').isdigit() else 0
                if kind != 'trouble' and seconds > SLOW:
                    pace.trouble()
                    seen['slow'] += 1
            if kind in ('tile', 'empty', 'recorded'):
                db.execute('INSERT INTO tiles VALUES (?, ?, ?, ?)', (tile, status, headers.get('Last-Modified'), detail))
                report[kind] += 1
                report['bytes'] += len(detail or b'')
                if kind != 'recorded':  # a 400 or 410 for everything is no good answer either
                    last_ok = now
            elif kind == 'refused':
                failure = failure or f'MML refused the key ({detail}); stopped'
                stop.set()
            else:
                seen[detail] += 1
                pace.trouble(min(pause, GIVE_UP))
                if detail == 'HTTP 429' and pace.floor_since is not None and now - pace.floor_since > GIVE_UP:
                    failure = failure or f'MML kept limiting us at {FLOOR} request a second; stopped'
                    stop.set()
                if attempt < ATTEMPTS and not stop.is_set():
                    work.put((tile, attempt + 1))
                    outstanding += 1
                else:
                    report['unanswered'] += 1
            if now - last_ok > GIVE_UP and not stop.is_set():
                failure = f'No good answer from MML in {GIVE_UP} s; stopped'
                stop.set()
            if time.time() >= until and not stop.is_set():
                print('Deadline: stopping; the next run goes on from here', flush=True)
                stop.set()
            if now - last_commit >= 10:
                db.commit()
                last_commit = now
            if now - last_print >= 60:
                print(json.dumps({'held': len(held) + report['tile'] + report['empty'] + report['recorded'], 'of': total,
                                  'rate': round(pace.rate, 2), 'per_second': round((answered - printed) / (now - last_print), 1),
                                  'trouble': dict(seen), **report}), flush=True)
                last_print, printed = now, answered
    finally:
        stop.set()
        for _ in threads:
            work.put(None)
        if report['tile'] + report['empty'] + report['recorded']:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('fetched_to', ?)", (stamp(datetime.now(timezone.utc)),))
        db.commit()
        held_now = db.execute('SELECT COUNT(*) FROM tiles').fetchone()[0]
        db.close()
        elapsed = time.monotonic() - began
        summary = json.dumps({'held': held_now, 'of': total, 'trouble': dict(seen), 'seconds': round(elapsed),
                              'per_second': round(answered / max(elapsed, 1e-9), 1), **report}, sort_keys=True)
        print(summary, flush=True)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
                out.write(f"changed={'true' if held_now > len(held) else 'false'}\n")
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out:
                out.write(f'### NLS background map: fetch\n\n```json\n{summary}\n```\n')
    if failure:
        raise Refused(failure)
    return report


def sample():
    """The hand check: Helsinki's tile at each zoom, with what MML says about it. Each must be a vector tile that
    names Helsinki, or the fetch doesn't start."""
    request, wrong = MML(os.environ.get('MML_API_KEY')), []
    for z in ZOOMS:
        x, y = column(HELSINKI[0], z), row(HELSINKI[1], z)
        status, headers, body, seconds = request(z, x, y)
        kind, _ = answer(status, headers, body)
        print(json.dumps({'tile': f'{z}/{x}/{y}', 'status': status, 'seconds': round(seconds, 3), 'answer': kind,
                          'bytes': len(body), 'gzip_bytes': len(gzip.compress(body, 6, mtime=0)),
                          'helsinki': b'Helsinki' in body, 'layers': layers(body) if kind == 'tile' else None}
                         | {name: headers.get(name) for name in ('Content-Type', 'Content-Length', 'Content-Encoding',
                                                                 'Last-Modified', 'Cache-Control', 'ETag')}), flush=True)
        if kind != 'tile' or b'Helsinki' not in body:
            wrong.append(f'{z}/{x}/{y}')
        time.sleep(1)
    require(not wrong, f"Helsinki's tiles {wrong} are not what MML should answer; the fetch doesn't start")


def pack(path, out, now=None):
    """The release: one PMTiles archive of every tile (404s as empty tiles), tiles.tsv.gz with each tile's status and
    Last-Modified, the licence and the manifest. Every tile of the box must have its answer."""
    now, out = now or datetime.now(timezone.utc), Path(out)
    db = store(path)
    meta = dict(db.execute('SELECT key, value FROM meta'))
    box = tuple(json.loads(meta['box']))
    expected = tile_ids(box)
    held = [tile for (tile,) in db.execute('SELECT id FROM tiles ORDER BY id')]
    require(held == expected, f'{len(set(expected) - set(held))} tiles have no answer yet (and {len(set(held) - set(expected))} '
            "lie outside the box): dispatch the workflow again with this run's ID as store")
    out.mkdir(parents=True, exist_ok=True)
    statuses, lines = Counter(), [b'z\tx\ty\tstatus\tlast_modified\n']
    with open(out / ARCHIVE, 'wb') as stream:
        writer = Writer(stream)
        for tile, status, modified, data in db.execute('SELECT id, status, modified, data FROM tiles ORDER BY id'):
            statuses[str(status)] += 1
            z, x, y = tileid_to_zxy(tile)
            lines.append(f'{z}\t{x}\t{y}\t{status}\t{modified or ""}\n'.encode())
            if status in (200, 404):
                writer.write_tile(tile, data if status == 200 else EMPTY)
        fetched = meta.get('fetched_to', stamp(now))
        attribution = f'© Maanmittauslaitos, Maastotietokanta {fetched[5:7]}/{fetched[:4]}'
        writer.finalize({'tile_type': TileType.MVT, 'tile_compression': Compression.GZIP,
                         'min_lon_e7': round(box[0] * 1e7), 'min_lat_e7': round(box[1] * 1e7),
                         'max_lon_e7': round(box[2] * 1e7), 'max_lat_e7': round(box[3] * 1e7)},
                        {'name': 'MML taustakartta v21', 'attribution': attribution, 'source': SOURCE,
                         'license': 'CC BY 4.0', 'fetched_from': meta.get('fetched_from'), 'fetched_to': fetched})
    (out / TABLE).write_bytes(gzip.compress(b''.join(lines), 6, mtime=0))
    (out / LICENSE).write_text(
        f'Background map vector tiles (taustakartta v21): {attribution}, National Land Survey of Finland.\n'
        'Licence: Creative Commons Attribution 4.0 International, https://creativecommons.org/licenses/by/4.0/\n'
        f'Source: {SOURCE}, fetched from {meta.get("fetched_from")} to {fetched}.\n'
        'The tiles are MML\'s own, gzipped and packed into PMTiles; nothing in them is changed.\n')
    # The archive must give back exactly what was fetched, tile for tile
    with open(out / ARCHIVE, 'rb') as stream:
        rows = db.execute('SELECT id, status, data FROM tiles WHERE status IN (200, 404) ORDER BY id')
        for ((z, x, y), data), (tile, status, held_data) in zip(all_tiles(MmapSource(stream)), rows, strict=True):
            require(zxy_to_tileid(z, x, y) == tile and data == (held_data if status == 200 else EMPTY),
                    f'The archive differs at {z}/{x}/{y}')
    files = [{'path': name, 'bytes': (out / name).stat().st_size, 'sha256': publish.digest_file(out / name)}
             for name in (ARCHIVE, TABLE, LICENSE)]
    manifest = {'schema': 1, 'release': now.strftime('%Y%m%dT%H%M%SZ'), 'built_at': stamp(now), 'source': SOURCE,
                'fetched_from': meta.get('fetched_from'), 'fetched_to': fetched, 'attribution': attribution,
                'license': 'CC BY 4.0', 'zooms': [ZOOMS[0], ZOOMS[-1]], 'bounds': list(box), 'tiles': dict(statuses),
                'datasets': {'taustakartta': {'url': ARCHIVE, 'bytes': files[0]['bytes'], 'sha256': files[0]['sha256']}},
                'files': files}
    publish.validate(manifest)
    (out / 'manifest.json').write_bytes(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
    db.close()
    print(json.dumps({k: manifest[k] for k in ('release', 'tiles', 'attribution')} | {'bytes': files[0]['bytes']}), flush=True)
    return manifest


def folder_url(osm_url):
    """polku-mml's public URL, beside polku-osm's."""
    require(osm_url.endswith('/polku-osm'), 'OSM_PUBLIC_URL must end in /polku-osm')
    return osm_url.removesuffix('/polku-osm') + '/' + FOLDER


@contextmanager
def mml_host():
    """The pinned-key SFTP login, in polku-mml beside polku-osm, and the folder's public URL."""
    config = publish.settings()
    url = folder_url(config[4])
    with publish.connection(config) as ftp:
        ftp.cwd('..')
        if FOLDER not in maintenance.children(ftp):
            ftp.mkd(FOLDER)
        require(ftp.kind(FOLDER) == 'directory', f'{FOLDER} needs a real directory')
        ftp.cwd(FOLDER)
        yield ftp, url


def remove_tree(ftp, base):
    """Only what this job writes."""
    for name in maintenance.children(ftp, base):
        require(ftp.kind(f'{base}/{name}') == 'file' and name in {ARCHIVE, TABLE, LICENSE, 'manifest.json', '_run.json'},
                f'Unexpected file {base}/{name}; inspect by hand')
        ftp.remove(f'{base}/{name}')
    ftp.rmd(base)


def upload(directory):
    """Upload the release into staging (going on from a stopped upload of the same release), read every file back,
    rename it into releases/, check it over HTTPS and name it in candidates/current.json."""
    require(os.environ.get('OSM_ALLOW_PUBLISH') == 'true', 'Publication requires approved OSM_ALLOW_PUBLISH=true')
    directory = Path(directory)
    raw, manifest = publish.local_release(directory)
    release, hosting = manifest['release'], Path(__file__).with_name('mml.htaccess').read_bytes()
    with mml_host() as (ftp, url):
        if '.htaccess' not in maintenance.children(ftp):
            ftp.storbinary('STOR .htaccess', io.BytesIO(hosting))
        require(publish.retrieve(ftp, '.htaccess') == hosting, f'{FOLDER}/.htaccess differs from mml.htaccess; change it by hand')
        if 'candidates' in maintenance.children(ftp) and 'current.json' in maintenance.children(ftp, 'candidates'):
            old = json.loads(publish.retrieve(ftp, 'candidates/current.json'))['bounds']
            new = manifest['bounds']
            # A test box never replaces a larger copy: retention would then remove that copy
            require(new[0] <= old[0] and new[1] <= old[1] and new[2] >= old[2] and new[3] >= old[3],
                    f'The candidate covers {old}, more than this release; upload a box that covers it')
        for name in ('releases', 'candidates'):
            if name not in maintenance.children(ftp):
                ftp.mkd(name)
        if release in maintenance.children(ftp, 'releases'):
            stage = f'releases/{release}'  # renamed before the run stopped: verified again below
        else:
            stage = STAGE
            if STAGE in maintenance.children(ftp):
                names = maintenance.children(ftp, STAGE)
                run = json.loads(publish.retrieve(ftp, f'{STAGE}/_run.json')) if '_run.json' in names else {}
                if run.get('release') != release:
                    remove_tree(ftp, STAGE)
            if STAGE not in maintenance.children(ftp):
                ftp.mkd(STAGE)
                ftp.storbinary(f'STOR {STAGE}/_run.json', io.BytesIO(json.dumps({'release': release}).encode()))
            names = maintenance.children(ftp, STAGE)
            have = {f['path']: ftp.size(f"{STAGE}/{f['path']}") if f['path'] in names else None for f in manifest['files']}
            incoming = sum(f['bytes'] - (have[f['path']] or 0) for f in manifest['files'])
            require(maintenance.usage(ftp) + incoming <= BUDGET, f'{FOLDER} storage budget exceeded (30 GiB)')
            for f in manifest['files']:
                path, size = f"{STAGE}/{f['path']}", have[f['path']]
                if size is not None and size > f['bytes']:
                    ftp.remove(path)
                    size = None
                if size != f['bytes']:
                    with open(directory / f['path'], 'rb') as body:
                        body.seek(size or 0)
                        ftp.storbinary('STOR ' + path, body, rest=size)
            ftp.storbinary(f'STOR {STAGE}/manifest.json', io.BytesIO(raw))
        try:
            samples = publish.verify_remote(ftp, stage, manifest, raw)
        except ValueError:
            if stage == STAGE:
                remove_tree(ftp, STAGE)  # a resumed file that doesn't hash right: the next run uploads it whole
            raise
        if stage == STAGE:
            ftp.remove(f'{STAGE}/_run.json')
            require(set(maintenance.children(ftp, STAGE)) == {f['path'] for f in manifest['files']} | {'manifest.json'},
                    'Unexpected files in staging')
            ftp.rename(STAGE, f'releases/{release}')
        publish.probe(url, manifest, raw, samples)
        publish.replace_pointer(ftp, raw, 'candidates/current.json')
        require(publish.get(url + '/candidates/current.json') == raw, 'Public candidate pointer differs')
        live = json.loads(publish.retrieve(ftp, 'current.json'))['release'] if 'current.json' in maintenance.children(ftp) else None
        for folder in maintenance.children(ftp, 'releases'):
            if folder not in {release, live}:
                remove_tree(ftp, 'releases/' + folder)
    print(f'Candidate {release}: {url}/releases/{release}/manifest.json', flush=True)


def candidate():
    """The uploaded candidate built after the live release, for Polku's checks, or None. Reads only public HTTP."""
    url = folder_url(publish.public_url(os.environ.get('OSM_PUBLIC_URL', '')))
    raw = publish.get(url + '/candidates/current.json')
    manifest = publish.validate(json.loads(raw))
    try:
        live = json.loads(publish.get(url + '/current.json'))
    except HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        live = None  # nothing promoted yet
    if live and manifest['built_at'] <= live['built_at']:
        return None
    location = f"{url}/releases/{manifest['release']}/manifest.json"
    require(publish.get(location, immutable=True) == raw, 'Candidate pointer differs from its release')
    return {'release': manifest['release'], 'manifest': location, 'sha256': hashlib.sha256(raw).hexdigest()}


def put_back(ftp, previous):
    """The live pointer as it was: the saved bytes, or none at all, so the app reads MML's own service."""
    if previous is None:
        ftp.remove('current.json')
    else:
        publish.replace_pointer(ftp, previous)


def promote(release, sha256, directory):
    """Make the candidate Polku checked live: current.json gets the manifest's same bytes. The pointer it replaces goes
    to DIRECTORY/previous.json (null where there was none), for restore."""
    require(os.environ.get('OSM_ALLOW_PUBLISH') == 'true', 'Publication requires approved OSM_ALLOW_PUBLISH=true')
    require(publish.NAME.fullmatch(release) and re.fullmatch('[a-f0-9]{64}', sha256), 'Invalid candidate')
    with mml_host() as (ftp, url):
        base = f'releases/{release}'
        raw = publish.retrieve(ftp, base + '/manifest.json')
        require(hashlib.sha256(raw).hexdigest() == sha256, 'Candidate manifest differs from the one checked')
        manifest = publish.validate(json.loads(raw))
        require(manifest['release'] == release, 'Candidate release differs')
        # The upload hashed every file and is their only writer, so sizes find what changed since
        names = maintenance.children(ftp, base)
        for f in manifest['files']:
            require(f['path'] in names and ftp.size(f"{base}/{f['path']}") == f['bytes'], 'Candidate file is missing or differs: ' + f['path'])
        previous = publish.retrieve(ftp, 'current.json') if 'current.json' in maintenance.children(ftp) else None
        if previous == raw:
            return  # promoted already
        require(not previous or manifest['built_at'] > json.loads(previous)['built_at'], 'Candidate is not newer than the live release')
        publish.probe(url, manifest, raw)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'previous.json').write_bytes(previous or b'null')
        publish.replace_pointer(ftp, raw)
        try:
            require(publish.get(url + '/current.json') == raw, 'Promoted pointer differs')
        except Exception:
            put_back(ftp, previous)
            raise


def restore(directory, expected_current):
    """Back to the pointer promote replaced, unless another release went live meanwhile."""
    require(os.environ.get('OSM_ALLOW_PUBLISH') == 'true', 'Publication requires approved OSM_ALLOW_PUBLISH=true')
    previous = Path(directory, 'previous.json').read_bytes()
    with mml_host() as (ftp, url):
        live = json.loads(publish.retrieve(ftp, 'current.json'))['release'] if 'current.json' in maintenance.children(ftp) else None
        require(live == expected_current, 'Live release changed; do not roll back another publication')
        put_back(ftp, None if previous == b'null' else previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('sample')
    run = commands.add_parser('fetch')
    run.add_argument('store', type=Path)
    run.add_argument('--box', default=','.join(map(str, BOX)), help='west,south,east,north in degrees')
    run.add_argument('--until', type=float, default=math.inf, help='stop cleanly at this time (epoch seconds)')
    run.add_argument('--limit', type=int, help='at most this many tiles in this run')
    packing = commands.add_parser('pack')
    packing.add_argument('store', type=Path)
    packing.add_argument('out', type=Path)
    commands.add_parser('upload').add_argument('directory', type=Path)
    commands.add_parser('candidate')
    promotion = commands.add_parser('promote')
    promotion.add_argument('release')
    promotion.add_argument('--sha256', required=True, help="the checked manifest's SHA-256, from `candidate`")
    promotion.add_argument('--directory', type=Path, required=True, help='where the replaced pointer is kept for restore')
    back = commands.add_parser('restore')
    back.add_argument('--directory', type=Path, required=True, help="promote's directory")
    back.add_argument('--expected-current', required=True, help='only if this release is live: never undo another publication')
    args = parser.parse_args()
    try:
        if args.command == 'sample':
            sample()
        elif args.command == 'fetch':
            box = tuple(float(value) for value in args.box.split(','))
            require(len(box) == 4 and box[0] < box[2] and box[1] < box[3], 'The box is west,south,east,north')
            args.store.parent.mkdir(parents=True, exist_ok=True)
            fetch(args.store, box, args.until, args.limit)
        elif args.command == 'pack':
            pack(args.store, args.out)
        elif args.command == 'upload':
            upload(args.directory)
        elif args.command == 'candidate':
            print(json.dumps(candidate() or {}, sort_keys=True))
            return
        elif args.command == 'promote':
            promote(args.release, args.sha256, args.directory)
        else:
            restore(args.directory, args.expected_current)
    except Exception as error:
        key = os.environ.get('MML_API_KEY')
        parser.exit(1, f"{args.command} failed: {str(error).replace(key, '[key]') if key else error}\n")
    print(f'{args.command}: done')


if __name__ == '__main__':
    main()
