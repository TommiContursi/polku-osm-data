#!/usr/bin/env python3
"""A copy of Digiroad's traffic volumes: the roads of 800 or more vehicles a day, as one zoom-11 PMTiles archive of
JSON tiles, uploaded as a candidate in polku-digiroad beside polku-osm.

The source is Väylävirasto's WFS layer dr_liikennemaara, CC BY 4.0. Väylävirasto's updates to it ended on 2026-09-18,
when Fintraffic took Digiroad over, so the copy is made by hand, and again only when a source with newer data exists.
The fetch asks for one page at a time, with a pause between pages and one retry after a longer one."""

import argparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import gzip
import http.client
import json
from pathlib import Path
import sys
import time
import urllib.request
from urllib.parse import urlencode

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from tools.osm_data import mml, publish
from tools.osm_data.publish import require
from tools.osm_data.routing import stamp

SOURCE = 'https://avoinapi.vaylapilvi.fi/vaylatiedot/digiroad/ows'
LAYER = 'digiroad:dr_liikennemaara'
AGENT = 'polku-osm-data/1.0 (+https://github.com/TommiContursi/polku-osm-data)'
BUSY = 800  # vehicles a day
ZOOM = 11
PAGE, PAUSE, BACK_OFF = 5000, 5, 300  # features a request, and seconds between pages and before the one retry
FOLDER, ARCHIVE = 'polku-digiroad', 'traffic.pmtiles'
BUDGET = 1024**3  # the copy is about 10 MB
ATTRIBUTION = 'Traffic volumes: © Fintraffic / Digiroad, from Väylävirasto\'s open data, CC BY 4.0'
LICENSE = 'CC BY 4.0'
LICENSE_TEXT = f'''Traffic volumes from Digiroad, © Fintraffic, from the open data of Väylävirasto (the Finnish Transport
Infrastructure Agency), licensed under Creative Commons Attribution 4.0 International:
https://creativecommons.org/licenses/by/4.0/

Source: {SOURCE}, layer {LAYER}. Changed: only roads of {BUSY} or more vehicles a day, coordinates rounded to six
decimal places without heights, and cut into map tiles.
'''


def page(start, sleep=time.sleep):
    """One page of busy roads from start, in WGS84, ordered by ID so the pages don't overlap."""
    query = urlencode({'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature', 'typeNames': LAYER,
                       'outputFormat': 'application/json', 'srsName': 'EPSG:4326', 'propertyName': 'arvo,geom,muokkauspv',
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
    """Every busy road, {id: feature}, and the newest edit among them: the data's own time. The count must be the WFS's
    own."""
    roads, matched, edited = {}, None, []
    while matched is None or len(roads) < matched:
        if matched is not None:
            sleep(PAUSE)
        body = page(len(roads), sleep)
        require(type(body.get('numberMatched')) is int and matched in (None, body['numberMatched']),
                'Digiroad changed its count during the fetch')
        matched = body['numberMatched']
        require(body.get('features'), f'Digiroad gave {len(roads)} of {matched} busy roads')
        for feature in body['features']:
            properties = feature.get('properties', {})
            geometry, aadt = feature.get('geometry') or {}, properties.get('arvo')
            require(type(aadt) is int and aadt >= BUSY and geometry.get('type') in {'LineString', 'MultiLineString'},
                    'Unexpected Digiroad feature ' + str(feature.get('id')))
            if properties.get('muokkauspv'):  # Finnish time, as Digiroad writes it: 31.10.2020 13:48:30
                edited.append(datetime.strptime(properties['muokkauspv'], '%d.%m.%Y %H:%M:%S'))
            lines = [geometry['coordinates']] if geometry['type'] == 'LineString' else geometry['coordinates']
            road = int(feature['id'].rpartition('.')[2])
            require(road not in roads, f'Digiroad repeated road {road}')
            roads[road] = {'id': road, 'aadt': aadt,
                           'lines': [[[round(p[0], 6), round(p[1], 6)] for p in line] for line in lines]}
    require(len(roads) == matched, f'Digiroad gave {len(roads)} of {matched} busy roads')
    require(edited, 'Digiroad gave no edit times')
    return roads, stamp(max(edited).replace(tzinfo=ZoneInfo('Europe/Helsinki')))


def build(roads, edited, output, now=None):
    """The release: the archive, LICENSE.txt and a manifest as the OSM data's, so publish.py uploads it. Each road goes
    into every tile its extent touches, whole."""
    now = now or datetime.now(timezone.utc)
    output = Path(output)
    require(roads and not output.exists(), 'No roads, or the release folder exists')
    tiles, extents = {}, []
    for road in roads.values():
        points = [p for line in road['lines'] for p in line]
        extent = [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]
        for x in range(mml.column(extent[0], ZOOM), mml.column(extent[2], ZOOM) + 1):
            for y in range(mml.row(extent[3], ZOOM), mml.row(extent[1], ZOOM) + 1):
                tiles.setdefault(zxy_to_tileid(ZOOM, x, y), []).append(road)
        extents.append(extent)
    bounds = [min(e[0] for e in extents), min(e[1] for e in extents), max(e[2] for e in extents), max(e[3] for e in extents)]
    source = {'url': SOURCE, 'layer': LAYER, 'filter': f'arvo>={BUSY}', 'timestamp': edited}
    output.mkdir(parents=True)
    with (output / ARCHIVE).open('wb') as stream:
        writer = Writer(stream)
        for tile in sorted(tiles):
            elements = sorted(tiles[tile], key=lambda road: road['id'])
            raw = json.dumps({'schema': 1, 'elements': elements}, separators=(',', ':')).encode()
            require(len(raw) <= 32 * 1024 * 1024, 'Tile over the browser limit')  # as the OSM builder's
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('build', help='fetch the busy roads and write the release').add_argument('output', type=Path)
    commands.add_parser('upload', help='upload the release as the candidate (in cells with OSM_CELLS=true)').add_argument('directory', type=Path)
    commands.add_parser('install-hosting')
    args = parser.parse_args()
    try:
        if args.command == 'build':
            manifest = build(*fetch(), args.output)
            print(json.dumps({'release': manifest['release'], **manifest['datasets']['traffic']}))
        elif args.command == 'upload':
            publish.upload(args.directory, folder=FOLDER, budget=BUDGET)
        else:
            publish.install_hosting(FOLDER)
    except Exception as error:
        parser.exit(1, f'{args.command} failed: {error}\n')
    print(f'{args.command}: verified')


if __name__ == '__main__':
    main()
