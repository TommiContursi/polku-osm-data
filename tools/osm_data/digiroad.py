#!/usr/bin/env python3
"""A copy of four Digiroad layers: traffic volumes (the roads of 800 or more vehicles a day), paved roads, private roads
and speed limits. Each is a zoom-11 PMTiles archive of JSON tiles beside its features as the WFS served them, all
uploaded as one candidate in polku-digiroad beside polku-osm.

The source is Väylävirasto's WFS, CC BY 4.0. Väylävirasto's updates to it ended on 2026-09-18,
when Fintraffic took Digiroad over, so the copy is made by hand, and again only when a source with newer data exists.
The fetch asks for one page at a time, with a pause between pages and one retry after a longer one."""

import argparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import gzip
import hashlib
import http.client
import io
import json
import lzma
from pathlib import Path
import sys
import time
import urllib.request
from urllib.parse import urlencode

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from tools.osm_data import maintenance, mml, publish
from tools.osm_data.publish import require
from tools.osm_data.routing import stamp

SOURCE = 'https://avoinapi.vaylapilvi.fi/vaylatiedot/digiroad/ows'
AGENT = 'polku-osm-data/1.0 (+https://github.com/TommiContursi/polku-osm-data)'
BUSY = 800  # vehicles a day
ZOOM = 11
PAGE, PAUSE, BACK_OFF = 5000, 5, 300  # features a request, and seconds between pages and before the one retry
FOLDER = 'polku-digiroad'
BUDGET = 4 * 1024**3  # live, candidate and staging: three copies of about 1 GB
SPEEDS = {20, 30, 40, 50, 60, 70, 80, 90, 100, 120}  # km/h
# The paving classes of the style dr_paallystetty_tie: Asfaltti, Kivi, Sitomaton kulutuskerros, Muut päällysteluokat
# and Päällystetty, tyyppi tuntematon
SURFACES = {1: 'asphalt', 2: 'stone', 3: 'unbound', 4: 'other', 99: 'unknown'}
RESTRICTIONS = {'tieto toimitettu - rajoituksia': 'yes', 'tieto toimitettu - ei rajoituksia': 'no',
                'ei toimitettu': 'unreported'}
# Each dataset, private roads first (the slowest to fetch, and their key the least proven): its WFS layer, its filter,
# the unique field that orders its pages, and what its tiles keep of a feature's properties: what the layer's WMS
# style draws, or None for a value the style doesn't draw
DATASETS = {
    # style dr_yksityistietieto: whether the road's restrictions were reported, and if there are any
    'private': ('digiroad:tiekunnalliset_yksityistiet', None, 'link_id',
                lambda p: {'restrictions': RESTRICTIONS[p['lisatiedot']]} if p.get('lisatiedot') in RESTRICTIONS
                else None),
    'traffic': ('digiroad:dr_liikennemaara', f'arvo>={BUSY}', 'id',
                lambda p: {'aadt': p['arvo']} if type(p.get('arvo')) is int and p['arvo'] >= BUSY else None),
    # style dr_paallystetty_tie: the paving class, and state roads (hallinn_lk 1) from a smaller scale than others
    'paved': ('digiroad:dr_paallystetty_tie', None, 'id',
              lambda p: {'surface': SURFACES[p['arvo']], 'state': p['hallinn_lk'] == 1} if p.get('arvo') in SURFACES
              and type(p.get('hallinn_lk')) is int and p['hallinn_lk'] >= 1 else None),
    # the layer's default style: the limit
    'speed': ('digiroad:dr_nopeusrajoitus', None, 'id_pk',
              lambda p: {'limit': p['arvo']} if p.get('arvo') in SPEEDS else None),
}
ATTRIBUTION = '© Fintraffic / Digiroad, from Väylävirasto\'s open data, CC BY 4.0'
LICENSE = 'CC BY 4.0'
LICENSE_TEXT = f'''Traffic volumes, paved roads, private roads and speed limits from Digiroad, © Fintraffic, from the open
data of Väylävirasto (the Finnish Transport Infrastructure Agency), licensed under Creative Commons Attribution 4.0
International: https://creativecommons.org/licenses/by/4.0/

Source: {SOURCE}, layers dr_liikennemaara, dr_paallystetty_tie,
tiekunnalliset_yksityistiet and dr_nopeusrajoitus. The .jsonl.xz files hold their features as the service gave them,
one per line; of the traffic volumes, only the roads of {BUSY} or more vehicles a day. Changed in the map tiles: only
the properties the layers' styles draw, and coordinates rounded to six decimal places without heights.
'''


def page(name, start, sleep=time.sleep):
    """One page of the dataset's features from start, in WGS84, ordered by a unique field so the pages don't overlap."""
    layer, where, order, _ = DATASETS[name]
    query = urlencode({'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature', 'typeNames': layer,
                       'outputFormat': 'application/json', 'srsName': 'EPSG:4326', 'sortBy': order,
                       'count': PAGE, 'startIndex': start} | ({'cql_filter': where} if where else {}))
    for attempt in (1, 2):
        try:
            request = urllib.request.Request(f'{SOURCE}?{query}', headers={'User-Agent': AGENT})
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)
        except (OSError, ValueError, http.client.HTTPException) as error:  # HTTP errors and bodies cut short too
            if attempt == 2:
                raise RuntimeError(f'Digiroad {name} failed twice at feature {start} ({error})') from None
            print(f'Digiroad {name} at feature {start}: {error}; one retry in {BACK_OFF} s', flush=True)
            sleep(BACK_OFF)


def element(name, feature):
    """The feature as the dataset's tiles hold it: its ID, what its layer's style draws, and its lines in WGS84 to six
    decimal places without heights. A feature the style doesn't draw stops the copy."""
    properties, geometry = feature.get('properties', {}), feature.get('geometry') or {}
    kept = DATASETS[name][3](properties)
    require(kept is not None and geometry.get('type') in {'LineString', 'MultiLineString'},
            'Unexpected Digiroad feature ' + str(feature.get('id')))
    lines = [geometry['coordinates']] if geometry['type'] == 'LineString' else geometry['coordinates']
    key = feature['id'].rpartition('.')[2]
    return {'id': int(key) if key.isdigit() else key, **kept,
            'lines': [[[round(p[0], 6), round(p[1], 6)] for p in line] for line in lines]}


def union(boxes):
    """The box around boxes of west, south, east and north."""
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def fetch(name, directory, sleep=time.sleep):
    """Every feature of the dataset as the WFS serves it, one JSON line each, into <name>.jsonl.xz as its pages come.
    Each feature is checked as it comes, must come once, and the count must be the WFS's own."""
    seen, matched = set(), None
    with lzma.open(directory / f'{name}.jsonl.xz', 'wb') as raw:
        while matched is None or len(seen) < matched:
            if matched is not None:
                sleep(PAUSE)
            body = page(name, len(seen), sleep)
            require(type(body.get('numberMatched')) is int and matched in (None, body['numberMatched']),
                    'Digiroad changed its count during the fetch')
            matched = body['numberMatched']
            require(body.get('features'), f'Digiroad gave {len(seen)} of {matched} {name} features')
            for feature in body['features']:
                element(name, feature)
                require(feature['id'] not in seen, f'Digiroad repeated {feature["id"]}')
                seen.add(feature['id'])
                raw.write(json.dumps(feature, ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
            print(f'Digiroad {name}: {len(seen)} of {matched}', flush=True)
    require(len(seen) == matched, f'Digiroad gave {len(seen)} of {matched} {name} features')


def build(directory, now=None):
    """The release from the fetched features in directory, <name>.jsonl.xz: an archive per dataset, LICENSE.txt and a
    manifest as the OSM data's, naming the fetched features too, so publish.py uploads them all. Each feature goes into
    every tile its extent touches, whole."""
    now, directory = now or datetime.now(timezone.utc), Path(directory)
    datasets, files, boxes = {}, {}, []

    def entry(path):
        return {'path': path, 'bytes': (directory / path).stat().st_size,
                'sha256': publish.digest_file(directory / path)}

    for name, (layer, where, _, _) in DATASETS.items():
        tiles, extents, edited = {}, [], datetime.min
        with lzma.open(directory / f'{name}.jsonl.xz') as raw:
            for line in raw:
                feature = json.loads(line)
                road = element(name, feature)
                if feature['properties'].get('muokkauspv'):  # Finnish time, as Digiroad writes it: 31.10.2020 13:48:30
                    edited = max(edited, datetime.strptime(feature['properties']['muokkauspv'], '%d.%m.%Y %H:%M:%S'))
                points = [p for line in road['lines'] for p in line]
                extent = [min(p[0] for p in points), min(p[1] for p in points),
                          max(p[0] for p in points), max(p[1] for p in points)]
                for x in range(mml.column(extent[0], ZOOM), mml.column(extent[2], ZOOM) + 1):
                    for y in range(mml.row(extent[3], ZOOM), mml.row(extent[1], ZOOM) + 1):
                        tiles.setdefault(zxy_to_tileid(ZOOM, x, y), []).append(road)
                extents.append(extent)
        require(extents, f'No {name} features')
        bounds = union(extents)
        # the newest edit, where the layer has edit times: the private roads have none
        source = ({'url': SOURCE, 'layer': layer} | ({'filter': where} if where else {})
                  | ({'timestamp': stamp(edited.replace(tzinfo=ZoneInfo('Europe/Helsinki')))}
                     if edited > datetime.min else {}))
        archive, fetched = f'{name}.pmtiles', f'{name}.jsonl.xz'
        with (directory / archive).open('wb') as stream:
            writer = Writer(stream)
            for tile in sorted(tiles):
                elements = sorted(tiles[tile], key=lambda road: road['id'])
                body = json.dumps({'schema': 1, 'elements': elements}, separators=(',', ':')).encode()
                require(len(body) <= 32 * 1024 * 1024, 'Tile over the browser limit')  # as the OSM builder's
                writer.write_tile(tile, gzip.compress(body, 6, mtime=0))
            writer.finalize({'tile_type': TileType.UNKNOWN, 'tile_compression': Compression.GZIP,
                             'min_lon_e7': round(bounds[0] * 1e7), 'min_lat_e7': round(bounds[1] * 1e7),
                             'max_lon_e7': round(bounds[2] * 1e7), 'max_lat_e7': round(bounds[3] * 1e7)},
                            {'name': f'Polku Digiroad {name}', 'format': 'json', 'schema': 1,
                             'attribution': ATTRIBUTION, 'license': LICENSE, 'source': source})
        files[archive], files[fetched] = entry(archive), entry(fetched)
        datasets[name] = {'format': 'pmtiles-json', 'schema': 1, 'zoom': ZOOM, 'url': archive,
                          'bytes': files[archive]['bytes'], 'sha256': files[archive]['sha256'], 'tiles': len(tiles),
                          'features': len(extents), 'source': source, 'raw': fetched}
        boxes.append(bounds)
    edits = [d['source']['timestamp'] for d in datasets.values() if 'timestamp' in d['source']]
    require(edits, 'Digiroad gave no edit times')
    (directory / 'LICENSE.txt').write_text(LICENSE_TEXT, encoding='utf-8')
    files['LICENSE.txt'] = entry('LICENSE.txt')
    files = list(files.values())
    content = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    release = stamp(now).replace('-', '').replace(':', '') + '-' + content[:12]
    manifest = {'schema': 1, 'release': release, 'built_at': stamp(now),
                'source': {'url': SOURCE, 'timestamp': max(edits)}, 'bounds': union(boxes),
                'datasets': datasets, 'files': files, 'attribution': ATTRIBUTION, 'license': LICENSE}
    (directory / 'manifest.json').write_bytes(json.dumps(publish.validate(manifest), sort_keys=True).encode())
    return manifest


def install_hosting():
    """polku-digiroad and its .htaccess, polku-osm's own, once: an existing one is changed by hand."""
    config = publish.settings()
    require(config[4].endswith('/polku-osm'), 'OSM_PUBLIC_URL must end in /polku-osm')
    with publish.connection(config) as ftp:
        ftp.cwd('..')
        if FOLDER not in maintenance.children(ftp):
            ftp.mkd(FOLDER)
        require(ftp.kind(FOLDER) == 'directory', f'{FOLDER} needs a real directory')
        ftp.cwd(FOLDER)
        require('.htaccess' not in maintenance.children(ftp), f'{FOLDER}/.htaccess exists; change it by hand')
        ftp.storbinary('STOR .htaccess', io.BytesIO(Path(__file__).with_name('hosting.htaccess').read_bytes()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('fetch', help='fetch the four layers into a new folder').add_argument('output', type=Path)
    commands.add_parser('build', help='write the release in a folder of fetched features').add_argument('directory', type=Path)
    commands.add_parser('upload', help='upload the release as the candidate (in cells with OSM_CELLS=true)').add_argument('directory', type=Path)
    commands.add_parser('install-hosting')
    args = parser.parse_args()
    try:
        if args.command == 'fetch':
            args.output.mkdir(parents=True)
            for name in DATASETS:
                fetch(name, args.output)
        elif args.command == 'build':
            manifest = build(args.directory)
            print(json.dumps({'release': manifest['release'], 'datasets': manifest['datasets']}))
        elif args.command == 'upload':
            publish.upload(args.directory, folder=FOLDER, budget=BUDGET)
        else:
            install_hosting()
    except Exception as error:
        parser.exit(1, f'{args.command} failed: {error}\n')
    print(f'{args.command}: verified')


if __name__ == '__main__':
    main()
