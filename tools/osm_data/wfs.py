"""A copy of a WFS's layers, as digiroad.py and lipas.py make it: each layer's features fetched page by page into
<name>.jsonl.xz as served, then built into a PMTiles archive of JSON tiles at one zoom, each feature whole in every tile
its extent touches, beside LICENSE.txt and a manifest as the OSM data's, so publish.py uploads them all.

A product is a module with LABEL, SOURCE, SRS, ZOOM, PAGE, PAUSE, BACK_OFF, FOLDER, BUDGET, ATTRIBUTION, LICENSE,
LICENSE_TEXT, DATASETS ({name: (layer, filter or None, sort order, …)}), LEVELS ({name: (zooms below ZOOM, …)}: a
dataset's overview levels, each a dataset of its own, <name>-<zoom>) and these functions:
- element(name, feature): what its tiles keep of it, which stops the copy for a feature it doesn't expect;
- extent(element): its box in WGS84, west, south, east and north;
- rank(element): its place in a tile;
- edited(properties): when it was last edited, as an aware datetime, or None;
- overview(name, zoom, element): what a level's tiles keep of it, or None (with LEVELS only)."""

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import http.client
import json
import lzma
import os
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlencode

from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from tools.osm_data import mml, publish
from tools.osm_data.publish import require
from tools.osm_data.routing import stamp

AGENT = 'polku-osm-data/1.0 (+https://github.com/TommiContursi/polku-osm-data)'


def page(product, name, start, sleep=time.sleep):
    """One page of the dataset's features from start, ordered so the pages don't overlap."""
    layer, where, order = product.DATASETS[name][:3]
    query = urlencode({'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature', 'typeNames': layer,
                       'outputFormat': 'application/json', 'srsName': product.SRS, 'sortBy': order,
                       'count': product.PAGE, 'startIndex': start} | ({'cql_filter': where} if where else {}))
    for attempt in (1, 2):
        try:
            request = urllib.request.Request(f'{product.SOURCE}?{query}', headers={'User-Agent': AGENT})
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)
        except (OSError, ValueError, http.client.HTTPException) as error:  # HTTP errors and bodies cut short too
            if attempt == 2:
                raise RuntimeError(f'{product.LABEL} {name} failed twice at feature {start} ({error})') from None
            print(f'{product.LABEL} {name} at feature {start}: {error}; one retry in {product.BACK_OFF} s', flush=True)
            sleep(product.BACK_OFF)


def fetch(product, name, directory, sleep=time.sleep):
    """Every feature of the dataset as the WFS serves it, one JSON line each, into <name>.jsonl.xz as its pages come.
    Each feature is checked as it comes, must come once by its ID in the WFS, and the count must be the WFS's own."""
    seen, matched = set(), None
    with lzma.open(Path(directory) / f'{name}.jsonl.xz', 'wb') as raw:
        while matched is None or len(seen) < matched:
            if matched is not None:
                sleep(product.PAUSE)
            body = page(product, name, len(seen), sleep)
            require(type(body.get('numberMatched')) is int and matched in (None, body['numberMatched']),
                    f'{product.LABEL} changed its count during the fetch')
            matched = body['numberMatched']
            require(body.get('features'), f'{product.LABEL} gave {len(seen)} of {matched} {name} features')
            for feature in body['features']:
                product.element(name, feature)
                require(feature['id'] not in seen, f'{product.LABEL} repeated {feature["id"]}')
                seen.add(feature['id'])
                raw.write(json.dumps(feature, ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
            print(f'{product.LABEL} {name}: {len(seen)} of {matched}', flush=True)
    require(len(seen) == matched, f'{product.LABEL} gave {len(seen)} of {matched} {name} features')


def live(product, directory):
    """The live release's fetched features, <name>.jsonl.xz, saved from the host into directory and checked by their
    SHA-256, so build makes the release again without the WFS."""
    base = publish.product_url(publish.public_url(os.environ.get('OSM_PUBLIC_URL', '')), product.FOLDER)
    files = {f['path']: f for f in publish.validate(json.loads(publish.get(base + '/current.json')), True)['files']}
    for name in product.DATASETS:
        entry, digest = files.get(f'{name}.jsonl.xz'), hashlib.sha256()
        require(entry, f'The live release has no {name}.jsonl.xz')
        request = urllib.request.Request(f"{base}/{entry['url']}", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(request, timeout=300) as response, (Path(directory) / entry['path']).open('wb') as out:
            while block := response.read(1 << 20):
                digest.update(block)
                out.write(block)
        require(digest.hexdigest() == entry['sha256'], f"{entry['path']} differs from the live release's")
        print(f"{product.LABEL} {name}: {entry['bytes']} bytes from the live release", flush=True)


def union(boxes):
    """The box around boxes of west, south, east and north."""
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def build(product, directory, now):
    """The release from the fetched features in directory, <name>.jsonl.xz: an archive per dataset, LICENSE.txt and a
    manifest as the OSM data's, naming the fetched features too, so publish.py uploads them all."""
    directory, zoom = Path(directory), product.ZOOM
    datasets, files, boxes = {}, {}, []

    def entry(path):
        return {'path': path, 'bytes': (directory / path).stat().st_size,
                'sha256': publish.digest_file(directory / path)}

    def place(tiles, element, zoom):
        extent = product.extent(element)
        for x in range(mml.column(extent[0], zoom), mml.column(extent[2], zoom) + 1):
            for y in range(mml.row(extent[3], zoom), mml.row(extent[1], zoom) + 1):
                tiles.setdefault(zxy_to_tileid(zoom, x, y), []).append(element)
        return extent

    def write(name, zoom, tiles, extents, source, fetched):
        require(extents, f'No {name} features')
        bounds, archive = union(extents), f'{name}.pmtiles'
        with (directory / archive).open('wb') as stream:
            writer = Writer(stream)
            for tile in sorted(tiles):
                body = json.dumps({'schema': 1, 'elements': sorted(tiles[tile], key=product.rank)},
                                  separators=(',', ':')).encode()
                require(len(body) <= 32 * 1024 * 1024, 'Tile over the browser limit')  # as the OSM builder's
                writer.write_tile(tile, gzip.compress(body, 6, mtime=0))
            writer.finalize({'tile_type': TileType.UNKNOWN, 'tile_compression': Compression.GZIP,
                             'min_lon_e7': round(bounds[0] * 1e7), 'min_lat_e7': round(bounds[1] * 1e7),
                             'max_lon_e7': round(bounds[2] * 1e7), 'max_lat_e7': round(bounds[3] * 1e7)},
                            {'name': f'Polku {product.LABEL} {name}', 'format': 'json', 'schema': 1,
                             'attribution': product.ATTRIBUTION, 'license': product.LICENSE, 'source': source})
        files[archive] = entry(archive)
        datasets[name] = {'format': 'pmtiles-json', 'schema': 1, 'zoom': zoom, 'url': archive,
                          'bytes': files[archive]['bytes'], 'sha256': files[archive]['sha256'], 'tiles': len(tiles),
                          'features': len(extents), 'source': source, 'raw': fetched}
        boxes.append(bounds)

    for name, (layer, where, *_) in product.DATASETS.items():
        tiles, extents, edited, fetched = {}, [], None, f'{name}.jsonl.xz'
        levels = {z: ({}, []) for z in (product.LEVELS[name][0] if name in product.LEVELS else ())}
        with lzma.open(directory / fetched) as raw:
            for line in raw:
                feature = json.loads(line)
                element = product.element(name, feature)
                when = product.edited(feature['properties'])
                edited = max(edited, when) if edited and when else edited or when
                extents.append(place(tiles, element, zoom))
                for z, (level, kept) in levels.items():
                    if small := product.overview(name, z, element):
                        kept.append(place(level, small, z))
        source = ({'url': product.SOURCE, 'layer': layer} | ({'filter': where} if where else {})
                  | ({'timestamp': stamp(edited)} if edited else {}))
        write(name, zoom, tiles, extents, source, fetched)
        for z, (level, kept) in levels.items():
            write(f'{name}-{z}', z, level, kept, source, fetched)
        files[fetched] = entry(fetched)
    edits = [d['source']['timestamp'] for d in datasets.values() if 'timestamp' in d['source']]
    require(edits, f'{product.LABEL} gave no edit times')
    (directory / 'LICENSE.txt').write_text(product.LICENSE_TEXT, encoding='utf-8')
    files['LICENSE.txt'] = entry('LICENSE.txt')
    files = list(files.values())
    content = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    release = stamp(now).replace('-', '').replace(':', '') + '-' + content[:12]
    manifest = {'schema': 1, 'release': release, 'built_at': stamp(now),
                'source': {'url': product.SOURCE, 'timestamp': max(edits)}, 'bounds': union(boxes),
                'datasets': datasets, 'files': files, 'attribution': product.ATTRIBUTION, 'license': product.LICENSE}
    (directory / 'manifest.json').write_bytes(json.dumps(publish.validate(manifest), sort_keys=True).encode())
    return manifest


def main(product):
    """The product's commands: fetch its layers or save the live release's, build the release, upload it as the
    candidate, or install its folder."""
    parser = argparse.ArgumentParser(description=product.__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('fetch', help='fetch the layers into a new folder').add_argument('output', type=Path)
    commands.add_parser('live', help="save the live release's fetched features into a new folder").add_argument('output', type=Path)
    commands.add_parser('build', help='write the release in a folder of fetched features').add_argument('directory', type=Path)
    commands.add_parser('upload', help='upload the release as the candidate (in cells with OSM_CELLS=true)').add_argument('directory', type=Path)
    commands.add_parser('install-hosting')
    args = parser.parse_args()
    try:
        if args.command == 'fetch':
            args.output.mkdir(parents=True)
            for name in product.DATASETS:
                fetch(product, name, args.output)
        elif args.command == 'live':
            args.output.mkdir(parents=True)
            live(product, args.output)
        elif args.command == 'build':
            manifest = build(product, args.directory, datetime.now(timezone.utc))
            print(json.dumps({'release': manifest['release'], 'datasets': manifest['datasets']}))
        elif args.command == 'upload':
            publish.upload(args.directory, folder=product.FOLDER, budget=product.BUDGET)
        else:
            publish.install_hosting(product.FOLDER)
    except Exception as error:
        parser.exit(1, f'{args.command} failed: {error}\n')
    print(f'{args.command}: verified')
