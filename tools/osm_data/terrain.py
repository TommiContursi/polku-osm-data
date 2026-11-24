#!/usr/bin/env python3
"""A copy of Mapterhorn's terrain, zooms 0-12 over the Nordic and Baltic countries: its WebP terrarium tiles, byte for
byte, as one PMTiles archive a zoom, extracted from Mapterhorn's planet archive by HTTP Range and uploaded in cells
among the shared files, as a candidate in polku-terrain. Polku's terrain-promote.yml checks the candidate in the app
and promotes it. By hand, about once a year: Mapterhorn's planet changes rarely, and only the cells that changed go up.

Mapterhorn adds no licence of its own; each source's licence and credit is in its attribution.json, copied beside the
tiles."""

from pathlib import Path
import json
import sys
import urllib.request

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pmtiles.tile import Compression, TileType
from tools.osm_data import extract, mml
from tools.osm_data.publish import require

SOURCE = 'https://download.mapterhorn.com/planet.pmtiles'
SOURCES = 'https://download.mapterhorn.com/attribution.json'  # each source's licence and credit
BOX = (4.0, 53.8, 31.7, 71.3)  # west, south, east, north: Finland, Sweden, Norway, Denmark and the Baltic countries
ZOOMS = range(0, 13)
FOLDER = 'polku-terrain'
BUDGET = 16 * 1024**3  # 7.25 GB, and the cells a new release changes
CREDIT = '© Mapterhorn'
LICENSE = '''Terrain from Mapterhorn (https://mapterhorn.com), its tiles unchanged: WebP terrarium elevation tiles from
{source}, last modified {modified}, zooms {zooms}, over {box} (west, south, east, north).

Mapterhorn adds no licence of its own. Each part of the terrain keeps its source's licence and credit, listed in
attribution.json beside this file (a copy of {sources}) and at https://mapterhorn.com/attribution.
'''


def build(output, box=BOX, zooms=ZOOMS, remote=None, now=None):
    """The release: an archive a zoom, Mapterhorn's attribution.json, the licence and the manifest."""
    remote = remote or extract.Remote(SOURCE)
    h = remote.header
    require(h['tile_type'] == TileType.WEBP and h['tile_compression'] == Compression.NONE and h['max_zoom'] >= zooms[-1],
            f'{SOURCE} is no longer WebP terrain to zoom {zooms[-1]}')
    request = urllib.request.Request(SOURCES, headers={'User-Agent': mml.AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        sources = response.read()
    require(json.loads(sources), 'Mapterhorn listed no sources')
    modified = remote.modified()
    licence = LICENSE.format(source=SOURCE, modified=modified, zooms=f'{zooms[0]}-{zooms[-1]}', box=', '.join(map(str, box)),
                             sources=SOURCES)
    return extract.release(output, remote, box, zooms, 'terrain', 'Mapterhorn terrain',
                           {'attribution.json': sources, 'LICENSE.txt': licence.encode()},
                           {'source': {'url': SOURCE, 'timestamp': modified, 'etag': remote.version[1]},
                            'attribution': CREDIT, 'license': 'Each source\'s: attribution.json'}, now)


if __name__ == '__main__':
    extract.main(__doc__, FOLDER, BUDGET, BOX, build)
