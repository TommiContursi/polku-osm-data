#!/usr/bin/env python3
"""A copy of OpenFreeMap's base map, zooms 0-14 over the Nordic and Baltic countries: its vector tiles, byte for byte,
as one PMTiles archive a zoom, extracted by HTTP Range from the planet archive of the build OpenFreeMap's TileJSON names,
and uploaded in cells among the shared files, as a candidate in polku-basemap. Polku's basemap-promote.yml checks the
candidate in the app and promotes it. Monthly: OpenFreeMap's weekly builds change most of their tiles' bytes."""

import json
from pathlib import Path
import re
import sys
import urllib.request

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pmtiles.tile import Compression, TileType
from tools.osm_data import extract, mml, terrain
from tools.osm_data.publish import require

TILEJSON = 'https://tiles.openfreemap.org/planet'
PLANET = 'https://btrfs.openfreemap.com/areas/planet/{version}/tiles.pmtiles'
BOX = terrain.BOX
ZOOMS = range(0, 15)
FOLDER = 'polku-basemap'
BUDGET = 20 * 1024**3  # 6.23 GB, its predecessor and a candidate that changes most of its cells
CREDIT = 'OpenFreeMap © OpenMapTiles Data from OpenStreetMap'
LICENSE = '''Base map vector tiles from OpenFreeMap (https://openfreemap.org), its build {version}, its tiles unchanged:
from {source}, zooms {zooms}, over {box} (west, south, east, north).

The data is © OpenStreetMap contributors, under the Open Database License: https://www.openstreetmap.org/copyright
The tiles follow the OpenMapTiles schema, © OpenMapTiles, CC BY 4.0: https://www.openmaptiles.org/
'''


def version():
    """The build OpenFreeMap's TileJSON names, the one its tiles come from."""
    request = urllib.request.Request(TILEJSON, headers={'User-Agent': mml.AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        tiles = json.load(response)['tiles'][0]
    found = re.fullmatch(r'https://tiles\.openfreemap\.org/planet/(\d{8}_\d{6}_pt)/\{z\}/\{x\}/\{y\}\.pbf', tiles)
    require(found, f'Unexpected OpenFreeMap tile URL: {tiles}')
    return found[1]


def build(output, box=BOX, zooms=ZOOMS, remote=None, now=None):
    """The release: an archive a zoom, the licence and the manifest."""
    build_version = version()
    source = PLANET.format(version=build_version)
    remote = remote or extract.Remote(source)
    h = remote.header
    require(h['tile_type'] == TileType.MVT and h['tile_compression'] == Compression.GZIP and h['max_zoom'] >= zooms[-1],
            f'{source} is no longer gzipped vector tiles to zoom {zooms[-1]}')
    licence = LICENSE.format(version=build_version, source=source, zooms=f'{zooms[0]}-{zooms[-1]}', box=', '.join(map(str, box)))
    return extract.release(output, remote, box, zooms, 'basemap', 'OpenFreeMap base map', {'LICENSE.txt': licence.encode()},
                           {'source': {'url': source, 'tilejson': TILEJSON, 'version': build_version,
                                       'timestamp': remote.modified(), 'etag': remote.version[1]},
                            'attribution': CREDIT, 'license': 'ODbL; OpenMapTiles CC BY 4.0'}, now)


if __name__ == '__main__':
    extract.main(__doc__, FOLDER, BUDGET, BOX, build)
