#!/usr/bin/env python3
"""A copy of Mapterhorn's terrain, zooms 0-12 over the Nordic and Baltic countries: its WebP terrarium tiles, byte for
byte, as one PMTiles archive a zoom, extracted from Mapterhorn's planet archive by HTTP Range and uploaded in cells
among the shared files, as a candidate in polku-terrain. Polku's terrain-promote.yml checks the candidate in the app
and promotes it. By hand, about once a year: Mapterhorn's planet changes rarely, and only the cells that changed go up.

Mapterhorn adds no licence of its own; each source's licence and credit is in its attribution.json, copied beside the
tiles."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.request

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pmtiles.tile import Compression, TileType
from tools.osm_data import extract, mml, publish
from tools.osm_data.publish import require
from tools.osm_data.routing import stamp

SOURCE = 'https://download.mapterhorn.com/planet.pmtiles'
SOURCES = 'https://download.mapterhorn.com/attribution.json'  # each source's licence and credit
BOX = (4.0, 53.8, 31.7, 71.3)  # west, south, east, north: Finland, Sweden, Norway, Denmark and the Baltic countries
ZOOMS = range(0, 13)
FOLDER = 'polku-terrain'
BUDGET = 16 * 1024**3  # about 7.2 GB, and the cells a new release changes
CREDIT = '© Mapterhorn'
LICENSE = '''Terrain from Mapterhorn (https://mapterhorn.com), its tiles unchanged: WebP terrarium elevation tiles from
{source}, last modified {modified}, zooms {zooms}, over {box} (west, south, east, north).

Mapterhorn adds no licence of its own. Each part of the terrain keeps its source's licence and credit, listed in
attribution.json beside this file (a copy of {sources}) and at https://mapterhorn.com/attribution.
'''


def archive_name(z):
    return f'terrain-{z}.pmtiles'


def build(output, box=BOX, zooms=ZOOMS, remote=None, sources=None, now=None):
    """The release: an archive a zoom, Mapterhorn's attribution.json, the licence and the manifest."""
    now, output = now or datetime.now(timezone.utc), Path(output)
    require(not output.exists(), 'The release folder exists')
    remote = remote or extract.Remote(SOURCE)
    h = remote.header
    require(h['tile_type'] == TileType.WEBP and h['tile_compression'] == Compression.NONE and h['max_zoom'] >= zooms[-1],
            f'{SOURCE} is no longer WebP terrain to zoom {zooms[-1]}')
    if sources is None:
        request = urllib.request.Request(SOURCES, headers={'User-Agent': mml.AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            sources = response.read()
    require(json.loads(sources), 'Mapterhorn listed no sources')
    output.mkdir(parents=True)
    counts = {}
    for z in zooms:
        print(f'Zoom {z}', flush=True)
        counts[z] = extract.extract(remote, box, z, output / archive_name(z),
                                    {'name': f'Mapterhorn terrain, zoom {z}', 'attribution': CREDIT})
    modified = remote.modified()
    (output / 'attribution.json').write_bytes(sources)
    (output / 'LICENSE.txt').write_text(LICENSE.format(source=SOURCE, modified=modified, zooms=f'{zooms[0]}-{zooms[-1]}',
                                                       box=', '.join(map(str, box)), sources=SOURCES), encoding='utf-8')
    files = [{'path': name, 'bytes': (output / name).stat().st_size, 'sha256': publish.digest_file(output / name)}
             for name in [*map(archive_name, zooms), 'attribution.json', 'LICENSE.txt']]
    manifest = {'schema': 1, 'release': now.strftime('%Y%m%dT%H%M%SZ'), 'built_at': stamp(now),
                'source': {'url': SOURCE, 'timestamp': modified, 'etag': remote.version[1]},
                'attribution': CREDIT, 'license': 'Each source\'s: attribution.json', 'zooms': [zooms[0], zooms[-1]],
                'bounds': list(box),
                'datasets': {f'terrain-{z}': {'url': f['path'], 'bytes': f['bytes'], 'sha256': f['sha256'], 'zoom': z,
                                              'tiles': counts[z]} for z, f in zip(zooms, files)},
                'files': files}
    (output / 'manifest.json').write_bytes(json.dumps(publish.validate(manifest), sort_keys=True, separators=(',', ':')).encode())
    print(json.dumps({'release': manifest['release'], 'tiles': sum(counts.values()),
                      'bytes': sum(f['bytes'] for f in files[:len(counts)])}), flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    building = commands.add_parser('build', help="extract Mapterhorn's tiles in the box and write the release")
    building.add_argument('output', type=Path)
    building.add_argument('--box', default=','.join(map(str, BOX)), help='west,south,east,north in degrees')
    commands.add_parser('install-hosting', help=f'make {FOLDER} and put hosting.htaccess in it, once')
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
            box = tuple(float(value) for value in args.box.split(','))
            require(len(box) == 4 and box[0] < box[2] and box[1] < box[3], 'The box is west,south,east,north')
            build(args.output, box)
        elif args.command == 'install-hosting':
            publish.install_hosting(FOLDER)
        elif args.command == 'upload':
            require(os.environ.get('OSM_CELLS') == 'true', 'The copy goes up in cells: set OSM_CELLS=true')
            publish.upload(args.directory, folder=FOLDER, budget=BUDGET)
        elif args.command == 'candidate':
            found = publish.candidate(publish.product_url(publish.public_url(os.environ.get('OSM_PUBLIC_URL', '')), FOLDER))
            print(json.dumps(found or {}, sort_keys=True))
            return
        elif args.command == 'promote':
            args.directory.mkdir(parents=True, exist_ok=True)
            publish.promote_candidate(args.release, args.sha256, args.directory, folder=FOLDER)
        else:
            publish.restore(args.directory, args.expected_current, FOLDER)
    except Exception as error:
        parser.exit(1, f'{args.command} failed: {error}\n')
    print(f'{args.command}: done')


if __name__ == '__main__':
    main()
