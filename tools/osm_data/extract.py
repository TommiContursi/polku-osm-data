"""A box of a remote PMTiles archive, one zoom at a time, read by HTTP Range: the directory leaves its tiles lie in,
then the tiles themselves in runs of up to 32 MiB, one request at a time. Each zoom becomes a one-zoom archive of its
own, its tiles byte for byte the source's. The copies made this way (terrain.py, basemap.py) share their release and
their commands here."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import time
import urllib.request
from urllib.error import HTTPError

from pmtiles.reader import MmapSource, all_tiles
from pmtiles.tile import deserialize_directory, deserialize_header, find_tile, zxy_to_tileid
from pmtiles.writer import Writer

from tools.osm_data import mml, publish
from tools.osm_data.publish import require
from tools.osm_data.routing import stamp

RUN = 32 * 1024**2  # bytes a request at most
GAP = 256 * 1024  # bytes between two tiles read through rather than asked for in a request of their own
ATTEMPTS, BACK_OFF = 3, 60  # tries a request, and seconds before the next
DEPTH = 4  # directories a tile's entry lies under at most (PMTiles writers nest leaves one level deep)


class Remote:
    """The archive at url, read by Range; its header, and its Last-Modified and ETag, which every read must keep."""

    def __init__(self, url, sleep=time.sleep):
        self.url, self.sleep, self.version = url, sleep, None
        self.header = deserialize_header(self.read(0, 127))
        require(self.header['clustered'], f'{url} is not clustered')

    def read(self, offset, length):
        end = offset + length - 1
        for attempt in range(1, ATTEMPTS + 1):
            request = urllib.request.Request(self.url, headers={'User-Agent': mml.AGENT, 'Range': f'bytes={offset}-{end}',
                                                                 'Accept-Encoding': 'identity'})
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    whole = re.fullmatch(rf'bytes {offset}-{end}/(\d+)', response.headers.get('Content-Range', ''))
                    require(response.status == 206 and whole, f'{self.url}: not the Range asked for')
                    version = (response.headers.get('Last-Modified'), response.headers.get('ETag'), whole[1])
                    require(version[0], f'{self.url} has no Last-Modified')
                    require(self.version in (None, version), f'{self.url} changed during the extract')
                    self.version = version
                    body = response.read()
                    require(len(body) == length, f'{self.url}: {len(body)} of {length} bytes')
                    return body
            except (OSError, http.client.HTTPException) as error:  # HTTP errors and bodies cut short too
                if isinstance(error, HTTPError):
                    error.close()
                if isinstance(error, HTTPError) and error.code < 500 and error.code != 429 or attempt == ATTEMPTS:
                    raise RuntimeError(f'{self.url} at {offset}: {error}') from None
                print(f'{self.url} at {offset}: {error}; again in {BACK_OFF} s', flush=True)
                self.sleep(BACK_OFF)

    def modified(self):
        """The archive's Last-Modified, as the manifests write times."""
        return stamp(parsedate_to_datetime(self.version[0]))


def ids(box, z):
    """Every tile touching the box at zoom z, as sorted tile IDs."""
    west, south, east, north = box
    return sorted(zxy_to_tileid(z, x, y) for x in range(mml.column(west, z), mml.column(east, z) + 1)
                  for y in range(mml.row(north, z), mml.row(south, z) + 1))


def places(remote, wanted):
    """{tile id: (offset, length)} of each wanted tile the archive holds, reading only the leaves they lie in."""
    h, found, leaves = remote.header, {}, {}
    root = deserialize_directory(remote.read(h['root_offset'], h['root_length']))
    for tile in wanted:
        directory = root
        for _ in range(DEPTH):
            entry = find_tile(directory, tile)
            if entry is None:
                break
            if entry.run_length:
                found[tile] = (h['tile_data_offset'] + entry.offset, entry.length)
                break
            key = h['leaf_directory_offset'] + entry.offset
            if key not in leaves:
                leaves[key] = deserialize_directory(remote.read(key, entry.length))
            directory = leaves[key]
        else:
            raise ValueError(f'{remote.url}: leaves nested deeper than {DEPTH}')
    return found


def runs(spans):
    """The spans, (offset, length), gathered into reads: [(offset, length, [spans])] in the archive's order."""
    out = []
    for offset, length in sorted(set(spans)):
        if out and offset - (out[-1][0] + out[-1][1]) <= GAP and offset + length - out[-1][0] <= RUN:
            start = out[-1][0]
            out[-1] = (start, max(out[-1][1], offset + length - start), out[-1][2] + [(offset, length)])
        else:
            out.append((offset, length, [(offset, length)]))
    return out


def extract(remote, box, z, path, metadata):
    """The box's tiles at zoom z into a one-zoom archive at path, read back tile for tile; how many there are."""
    found = places(remote, ids(box, z))
    require(found, f'No tiles at zoom {z} in {box}')
    reads = runs(found.values())
    run_of = {span: read for read in reads for span in read[2]}
    left, held = Counter(found.values()), {}  # a tile the archive stores once for several places is held till its last
    digest = hashlib.sha256()
    with open(path, 'wb') as stream:
        writer = Writer(stream)
        for tile in sorted(found):
            span = found[tile]
            if span not in held:  # the archive is clustered: its reads come in the tiles' order
                start, length, spans = run_of[span]
                body = remote.read(start, length)
                held.update({s: body[s[0] - start:s[0] - start + s[1]] for s in spans if left[s] and s not in held})
            data = held[span]
            left[span] -= 1
            if not left[span]:
                del held[span]
            writer.write_tile(tile, data)
            digest.update(tile.to_bytes(8, 'big') + data)
        west, south, east, north = box
        writer.finalize({'tile_type': remote.header['tile_type'], 'tile_compression': remote.header['tile_compression'],
                         'min_lon_e7': round(west * 1e7), 'min_lat_e7': round(south * 1e7),
                         'max_lon_e7': round(east * 1e7), 'max_lat_e7': round(north * 1e7)}, metadata)
    back = hashlib.sha256()
    with open(path, 'rb') as stream:
        for (tz, x, y), data in all_tiles(MmapSource(stream)):
            back.update(zxy_to_tileid(tz, x, y).to_bytes(8, 'big') + data)
    require(back.digest() == digest.digest(), f'{path} differs from what was read')
    return len(found)


def release(output, remote, box, zooms, name, title, files, fields, now=None):
    """A copy's release in output: the box's tiles as `<name>-<z>.pmtiles`, a zoom each, titled `title` and the zoom;
    `files` ({name: bytes}) beside them; and the manifest, with `fields` (source, attribution, licence) in it."""
    now, output = now or datetime.now(timezone.utc), Path(output)
    require(not output.exists(), 'The release folder exists')
    output.mkdir(parents=True)
    counts = {}
    for z in zooms:
        print(f'Zoom {z}', flush=True)
        counts[z] = extract(remote, box, z, output / f'{name}-{z}.pmtiles',
                            {'name': f'{title}, zoom {z}', 'attribution': fields['attribution']})
    for path, body in files.items():
        (output / path).write_bytes(body)
    listed = [{'path': path, 'bytes': (output / path).stat().st_size, 'sha256': publish.digest_file(output / path)}
              for path in [*(f'{name}-{z}.pmtiles' for z in zooms), *files]]
    manifest = {'schema': 1, 'release': now.strftime('%Y%m%dT%H%M%SZ'), 'built_at': stamp(now), **fields,
                'zooms': [zooms[0], zooms[-1]], 'bounds': list(box),
                'datasets': {f'{name}-{z}': {'url': f['path'], 'bytes': f['bytes'], 'sha256': f['sha256'], 'zoom': z,
                                             'tiles': counts[z]} for z, f in zip(zooms, listed)},
                'files': listed}
    (output / 'manifest.json').write_bytes(json.dumps(publish.validate(manifest), sort_keys=True, separators=(',', ':')).encode())
    print(json.dumps({'release': manifest['release'], 'tiles': sum(counts.values()),
                      'bytes': sum(f['bytes'] for f in listed[:len(counts)])}), flush=True)
    return manifest


def main(doc, folder, budget, box, build):
    """A copy's commands: build (with `build(output, box)`), install-hosting, upload, candidate, promote and restore."""
    parser = argparse.ArgumentParser(description=doc)
    commands = parser.add_subparsers(dest='command', required=True)
    building = commands.add_parser('build', help='extract the tiles in the box and write the release')
    building.add_argument('output', type=Path)
    building.add_argument('--box', default=','.join(map(str, box)), help='west,south,east,north in degrees')
    commands.add_parser('install-hosting', help=f'make {folder} and put hosting.htaccess in it, once')
    commands.add_parser('upload', help='upload the release in cells as the candidate').add_argument('directory', type=Path)
    commands.add_parser('candidate', help='the candidate built after the live release, over public HTTP')
    promotion = commands.add_parser('promote')
    promotion.add_argument('release')
    promotion.add_argument('--sha256', required=True, help="the checked manifest's SHA-256, from `candidate`")
    promotion.add_argument('--directory', type=Path, required=True, help='where the replaced pointer is kept for restore')
    back = commands.add_parser('restore')
    back.add_argument('--directory', type=Path, required=True, help="promote's directory")
    back.add_argument('--expected-current', required=True, help='only if this release is live: never undo another publication')
    args = parser.parse_args()
    try:
        if args.command == 'build':
            given = tuple(float(value) for value in args.box.split(','))
            require(len(given) == 4 and given[0] < given[2] and given[1] < given[3], 'The box is west,south,east,north')
            build(args.output, given)
        elif args.command == 'install-hosting':
            publish.install_hosting(folder)
        elif args.command == 'upload':
            require(os.environ.get('OSM_CELLS') == 'true', 'The copy goes up in cells: set OSM_CELLS=true')
            publish.require_covers(folder, publish.validate(json.loads((args.directory / 'manifest.json').read_bytes())))
            publish.upload(args.directory, folder=folder, budget=budget)
        elif args.command == 'candidate':
            found = publish.candidate(publish.product_url(publish.public_url(os.environ.get('OSM_PUBLIC_URL', '')), folder), first=True)
            print(json.dumps(found or {}, sort_keys=True))
            return
        elif args.command == 'promote':
            args.directory.mkdir(parents=True, exist_ok=True)
            publish.promote_candidate(args.release, args.sha256, args.directory, folder=folder)
        else:
            publish.restore(args.directory, args.expected_current, folder)
    except Exception as error:
        parser.exit(1, f'{args.command} failed: {error}\n')
    print(f'{args.command}: done')
