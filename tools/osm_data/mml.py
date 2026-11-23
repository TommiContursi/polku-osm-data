#!/usr/bin/env python3
"""A copy of the National Land Survey of Finland's (MML) background-map vector tiles, taustakartta v21, zooms 11-14
over a box, fetched once at a paced rate, packed into one PMTiles archive a zoom and uploaded in cells among the shared
files, as the OSM data's, as a candidate in polku-mml. Polku's mml-promote.yml checks the candidate in the app and
promotes it into polku-mml/current.json, the release people get.

The tiles are CC BY 4.0. MML may limit a user whose volume disturbs its service, so the fetch halves its rate at any
sign of load and stops when MML refuses the key. Each tile's Last-Modified goes with the release, so a later update
can ask again with If-Modified-Since. The key (MML_API_KEY) is only ever put into requests: never printed or stored."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import http.client
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
TABLE, LICENSE = 'tiles.tsv.gz', 'LICENSE.txt'
FOLDER = 'polku-mml'
CREDIT = '© Maanmittauslaitos'  # in the archives: the dated credit, in the manifest, would change every cell
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


def archive_name(z):
    return f'taustakartta-{z}.pmtiles'


def pack(path, out, now=None):
    """The release: one PMTiles archive a zoom of every tile (404s as empty tiles), tiles.tsv.gz with each tile's status
    and Last-Modified, the licence and the manifest. Every tile of the box must have its answer."""
    now, out = now or datetime.now(timezone.utc), Path(out)
    db = store(path)
    meta = dict(db.execute('SELECT key, value FROM meta'))
    box = tuple(json.loads(meta['box']))
    expected = tile_ids(box)
    held = [tile for (tile,) in db.execute('SELECT id FROM tiles ORDER BY id')]
    require(held == expected, f'{len(set(expected) - set(held))} tiles have no answer yet (and {len(set(held) - set(expected))} '
            "lie outside the box): dispatch the workflow again with this run's ID as store")
    out.mkdir(parents=True, exist_ok=True)
    fetched = meta.get('fetched_to', stamp(now))
    attribution = f'{CREDIT}, Maastotietokanta {fetched[5:7]}/{fetched[:4]}'
    statuses, lines, writers = Counter(), [b'z\tx\ty\tstatus\tlast_modified\n'], {}
    for tile, status, modified, data in db.execute('SELECT id, status, modified, data FROM tiles ORDER BY id'):
        statuses[str(status)] += 1
        z, x, y = tileid_to_zxy(tile)
        lines.append(f'{z}\t{x}\t{y}\t{status}\t{modified or ""}\n'.encode())
        if status in (200, 404):
            if z not in writers:  # the ids come zoom by zoom
                writers[z] = Writer(open(out / archive_name(z), 'wb'))
            writers[z].write_tile(tile, data if status == 200 else EMPTY)
    for z, writer in writers.items():
        writer.finalize({'tile_type': TileType.MVT, 'tile_compression': Compression.GZIP,
                         'min_lon_e7': round(box[0] * 1e7), 'min_lat_e7': round(box[1] * 1e7),
                         'max_lon_e7': round(box[2] * 1e7), 'max_lat_e7': round(box[3] * 1e7)},
                        {'name': f'MML taustakartta v21, zoom {z}', 'attribution': CREDIT, 'source': SOURCE,
                         'license': 'CC BY 4.0'})
        writer.f.close()
    (out / TABLE).write_bytes(gzip.compress(b''.join(lines), 6, mtime=0))
    (out / LICENSE).write_text(
        f'Background map vector tiles (taustakartta v21): {attribution}, National Land Survey of Finland.\n'
        'Licence: Creative Commons Attribution 4.0 International, https://creativecommons.org/licenses/by/4.0/\n'
        f'Source: {SOURCE}, fetched from {meta.get("fetched_from")} to {fetched}.\n'
        'The tiles are MML\'s own, gzipped and packed into PMTiles; nothing in them is changed.\n')
    # The archives must give back exactly what was fetched, tile for tile
    rows = db.execute('SELECT id, status, data FROM tiles WHERE status IN (200, 404) ORDER BY id')
    for z in writers:
        with open(out / archive_name(z), 'rb') as stream:
            for (tz, x, y), data in all_tiles(MmapSource(stream)):
                tile, status, held_data = next(rows)
                require(zxy_to_tileid(tz, x, y) == tile and data == (held_data if status == 200 else EMPTY),
                        f'The archive differs at {tz}/{x}/{y}')
    require(next(rows, None) is None, 'The archives lack tiles')
    files = [{'path': name, 'bytes': (out / name).stat().st_size, 'sha256': publish.digest_file(out / name)}
             for name in [*map(archive_name, writers), TABLE, LICENSE]]
    manifest = {'schema': 1, 'release': now.strftime('%Y%m%dT%H%M%SZ'), 'built_at': stamp(now), 'source': SOURCE,
                'fetched_from': meta.get('fetched_from'), 'fetched_to': fetched, 'attribution': attribution,
                'license': 'CC BY 4.0', 'zooms': [ZOOMS[0], ZOOMS[-1]], 'bounds': list(box), 'tiles': dict(statuses),
                'datasets': {f'taustakartta-{z}': {'url': f['path'], 'bytes': f['bytes'], 'sha256': f['sha256'], 'zoom': z}
                             for z, f in zip(writers, files)},
                'files': files}
    publish.validate(manifest)
    (out / 'manifest.json').write_bytes(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
    db.close()
    print(json.dumps({k: manifest[k] for k in ('release', 'tiles', 'attribution')}
                     | {'bytes': sum(f['bytes'] for f in files[:len(writers)])}), flush=True)
    return manifest


def folder_url(osm_url):
    """polku-mml's public URL, beside polku-osm's."""
    require(osm_url.endswith('/polku-osm'), 'OSM_PUBLIC_URL must end in /polku-osm')
    return osm_url.removesuffix('/polku-osm') + '/' + FOLDER


def point_live(ftp):
    """polku-mml's live pointer in the OSM data's form (current.json's URLs relative to the folder), once: the copy's
    first releases named the manifest's own bytes."""
    if 'current.json' in maintenance.children(ftp):
        raw = publish.retrieve(ftp, 'current.json')
        try:
            publish.validate(json.loads(raw), pointed=True)
        except ValueError:
            publish.replace_pointer(ftp, json.dumps(publish.current(publish.validate(json.loads(raw))), sort_keys=True,
                                                    separators=(',', ':')).encode())


def upload(directory):
    """The release into polku-mml in cells among the shared files, as the OSM data's: a cell the host holds isn't sent
    again. A test box never replaces a larger candidate: retention would then remove that copy."""
    require(os.environ.get('OSM_CELLS') == 'true', 'The copy goes up in cells: set OSM_CELLS=true')
    manifest = publish.validate(json.loads(Path(directory, 'manifest.json').read_bytes()))
    old = public_bytes(folder_url(publish.public_url(os.environ.get('OSM_PUBLIC_URL', ''))) + '/candidates/current.json')
    if old is not None:
        old, new = json.loads(old)['bounds'], manifest['bounds']
        require(new[0] <= old[0] and new[1] <= old[1] and new[2] >= old[2] and new[3] >= old[3],
                f'The candidate covers {old}, more than this release; upload a box that covers it')
    config = publish.settings()
    with publish.connection(config) as ftp:
        publish.product_folder(ftp, config[4], FOLDER)
        point_live(ftp)
    publish.upload(directory, folder=FOLDER, budget=BUDGET)


def public_bytes(url):
    """A pointer's bytes over HTTPS, or None where there is none (404)."""
    try:
        return publish.get(url)
    except HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None


def restore(directory, expected_current):
    """Back to the pointer promote replaced, the one it saved in directory, unless another release went live meanwhile."""
    saved = sorted(Path(directory).glob('rollback-*.json'))
    require(len(saved) == 1, f'{len(saved)} saved pointers in {directory}; restore needs the one promote saved')
    publish.rollback(saved[0], expected_current, folder=FOLDER)


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
            found = publish.candidate(folder_url(publish.public_url(os.environ.get('OSM_PUBLIC_URL', ''))))
            print(json.dumps(found or {}, sort_keys=True))
            return
        elif args.command == 'promote':
            args.directory.mkdir(parents=True, exist_ok=True)
            publish.promote_candidate(args.release, args.sha256, args.directory, folder=FOLDER)
        else:
            restore(args.directory, args.expected_current)
    except Exception as error:
        key = os.environ.get('MML_API_KEY')
        parser.exit(1, f"{args.command} failed: {str(error).replace(key, '[key]') if key else error}\n")
    print(f'{args.command}: done')


if __name__ == '__main__':
    main()
