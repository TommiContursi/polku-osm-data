#!/usr/bin/env python3
"""A copy of Digiroad's traffic volumes: the roads of 800 or more vehicles a day that Polku draws as busy roads, as one
zoom-11 PMTiles archive of JSON tiles, uploaded as a candidate in polku-digiroad beside polku-osm.

The source is Väylävirasto's WFS layer dr_liikennemaara, CC BY 4.0. Väylävirasto's updates to it ended on 2026-09-18,
when Fintraffic took Digiroad over, so the copy is made by hand, and again only when a source with newer data exists.
The fetch asks for one page at a time, with a pause between pages and one retry after a longer one."""

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.request
from urllib.parse import urlencode

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from tools.osm_data import maintenance, publish
from tools.osm_data.publish import require
from tools.osm_data.routing import stamp

SOURCE = 'https://avoinapi.vaylapilvi.fi/vaylatiedot/digiroad/ows'
LAYER = 'digiroad:dr_liikennemaara'
AGENT = 'polku-osm-data/1.0 (+https://github.com/TommiContursi/polku-osm-data)'
BUSY = 800  # vehicles a day: the least Polku draws as a busy road
ZOOM = 11  # Polku reads the busy roads by zoom-11 tile
PAGE, PAUSE, BACK_OFF = 5000, 5, 300  # features a request, and seconds between pages and before the one retry
FOLDER, ARCHIVE = 'polku-digiroad', 'traffic.pmtiles'
ATTRIBUTION = 'Traffic volumes: Digiroad, © Väylävirasto, CC BY 4.0'
LICENSE = 'CC BY 4.0'
LICENSE_TEXT = f'''Traffic volumes from Digiroad, © Väylävirasto (the Finnish Transport Infrastructure Agency), licensed under
Creative Commons Attribution 4.0 International: https://creativecommons.org/licenses/by/4.0/

Source: {SOURCE}, layer {LAYER}. Changed: only roads of {BUSY} or more vehicles a day, coordinates rounded to six
decimal places without heights, and cut into map tiles.
'''


def page(start, sleep=time.sleep):
    """One page of busy roads from start, in WGS84, ordered by ID so the pages don't overlap."""
    query = urlencode({'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature', 'typeNames': LAYER,
                       'outputFormat': 'application/json', 'srsName': 'EPSG:4326', 'propertyName': 'arvo,geom',
                       'sortBy': 'id', 'cql_filter': f'arvo>={BUSY}', 'count': PAGE, 'startIndex': start})
    for attempt in (1, 2):
        try:
            request = urllib.request.Request(f'{SOURCE}?{query}', headers={'User-Agent': AGENT})
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)
        except (OSError, ValueError, http.client.HTTPException) as error:  # HTTP errors and bodies cut short too
            if attempt == 2:
                raise RuntimeError(f'Digiroad failed twice at feature {start} ({error})') from None
            print(f'Digiroad at feature {start}: {error}; one retry in {BACK_OFF} s', flush=True)
            sleep(BACK_OFF)


def fetch(sleep=time.sleep):
    """Every busy road, {id: feature}, and when the WFS served them. The count must be the WFS's own."""
    roads, matched, served = {}, None, None
    while matched is None or len(roads) < matched:
        if matched is not None:
            sleep(PAUSE)
        body = page(len(roads), sleep)
        require(type(body.get('numberMatched')) is int and matched in (None, body['numberMatched']),
                'Digiroad changed its count during the fetch')
        matched, served = body['numberMatched'], served or body.get('timeStamp')
        require(body.get('features'), f'Digiroad gave {len(roads)} of {matched} busy roads')
        for feature in body['features']:
            geometry, aadt = feature.get('geometry') or {}, feature.get('properties', {}).get('arvo')
            require(type(aadt) is int and aadt >= BUSY and geometry.get('type') in {'LineString', 'MultiLineString'},
                    'Unexpected Digiroad feature ' + str(feature.get('id')))
            lines = [geometry['coordinates']] if geometry['type'] == 'LineString' else geometry['coordinates']
            road = int(feature['id'].rpartition('.')[2])
            require(road not in roads, f'Digiroad repeated road {road}')
            roads[road] = {'id': road, 'aadt': aadt,
                           'lines': [[[round(p[0], 6), round(p[1], 6)] for p in line] for line in lines]}
    require(len(roads) == matched, f'Digiroad gave {len(roads)} of {matched} busy roads')
    return roads, served


def tile_range(lon, lat):
    side = 1 << ZOOM
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * side
    return min(side - 1, int((lon + 180) / 360 * side)), min(side - 1, int(y))


def build(roads, served, output, now=None):
    """The release: the archive, LICENSE.txt and a manifest as the OSM data's, so publish.py uploads it. Each road goes
    into every tile its extent touches, whole."""
    now = now or datetime.now(timezone.utc)
    output = Path(output)
    require(roads and not output.exists(), 'No roads, or the release folder exists')
    tiles, extents = {}, []
    for road in roads.values():
        points = [p for line in road['lines'] for p in line]
        extent = [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]
        (x0, y1), (x1, y0) = tile_range(*extent[:2]), tile_range(*extent[2:])
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                tiles.setdefault(zxy_to_tileid(ZOOM, x, y), []).append(road)
        extents.append(extent)
    bounds = [min(e[0] for e in extents), min(e[1] for e in extents), max(e[2] for e in extents), max(e[3] for e in extents)]
    source = {'url': SOURCE, 'layer': LAYER, 'filter': f'arvo>={BUSY}', 'timestamp': served}
    output.mkdir(parents=True)
    with (output / ARCHIVE).open('wb') as stream:
        writer = Writer(stream)
        for tile in sorted(tiles):
            elements = sorted(tiles[tile], key=lambda road: road['id'])
            raw = json.dumps({'schema': 1, 'elements': elements}, separators=(',', ':')).encode()
            require(len(raw) <= 32 * 1024 * 1024, 'Tile over the browser limit')  # Polku's reader refuses larger ones
            writer.write_tile(tile, gzip.compress(raw, 6, mtime=0))
        writer.finalize({'tile_type': TileType.UNKNOWN, 'tile_compression': Compression.GZIP,
                         'min_lon_e7': round(bounds[0] * 1e7), 'min_lat_e7': round(bounds[1] * 1e7),
                         'max_lon_e7': round(bounds[2] * 1e7), 'max_lat_e7': round(bounds[3] * 1e7)},
                        {'name': 'Polku Digiroad traffic volumes', 'format': 'json', 'schema': 1,
                         'attribution': ATTRIBUTION, 'license': LICENSE, 'source': source})
    (output / 'LICENSE.txt').write_text(LICENSE_TEXT, encoding='utf-8')
    files = [{'path': name, 'bytes': (output / name).stat().st_size, 'sha256': publish.digest_file(output / name)}
             for name in (ARCHIVE, 'LICENSE.txt')]
    release = stamp(now).replace('-', '').replace(':', '') + '-' + files[0]['sha256'][:12]
    manifest = {'schema': 1, 'release': release, 'built_at': stamp(now), 'source': source, 'bounds': bounds,
                'datasets': {'traffic': {'format': 'pmtiles-json', 'schema': 1, 'zoom': ZOOM, 'url': ARCHIVE,
                                         'bytes': files[0]['bytes'], 'sha256': files[0]['sha256'],
                                         'tiles': len(tiles), 'features': len(roads)}},
                'files': files, 'attribution': ATTRIBUTION, 'license': LICENSE}
    (output / 'manifest.json').write_bytes(json.dumps(publish.validate(manifest), sort_keys=True).encode())
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
    commands.add_parser('build', help='fetch the busy roads and write the release').add_argument('output', type=Path)
    commands.add_parser('upload', help='upload the release as the candidate, in cells').add_argument('directory', type=Path)
    commands.add_parser('install-hosting')
    args = parser.parse_args()
    try:
        if args.command == 'build':
            manifest = build(*fetch(), args.output)
            print(json.dumps({'release': manifest['release'], **manifest['datasets']['traffic']}))
        elif args.command == 'upload':
            os.environ['OSM_CELLS'] = 'true'  # from its first release: only the cells that changed go up
            publish.upload(args.directory, folder=FOLDER)
        else:
            install_hosting()
    except Exception as error:
        parser.exit(1, f'{args.command} failed: {error}\n')
    print(f'{args.command}: verified')


if __name__ == '__main__':
    main()
