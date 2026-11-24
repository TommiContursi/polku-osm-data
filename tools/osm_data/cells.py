"""An archive cut into cells: plain PMTiles files of at most 1 MiB, by a quadtree, and an index naming each cell by its
SHA-256. A cell's bytes depend only on its tiles and its quadrant, so it keeps its name while they don't change, and
an upload sends only the cells that changed."""

from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import math

from pmtiles.reader import MemorySource, MmapSource, Reader
from pmtiles.tile import Compression, TileType, deserialize_directory, deserialize_header, tileid_to_zxy
from pmtiles.writer import Writer

from tools.osm_data.publish import require

TARGET = 1 << 20
KINDS = {(TileType.UNKNOWN, Compression.GZIP), (TileType.MVT, Compression.GZIP), (TileType.WEBP, Compression.NONE)}
KEPT = ('name', 'format', 'schema', 'attribution', 'license')  # the metadata that doesn't change from week to week


def directory(get_bytes, header):
    """Every tile's place in the archive, {tile id: (offset, length)}, from its directories alone."""
    tiles = {}
    def walk(offset, length):
        for entry in deserialize_directory(get_bytes(offset, length)):
            if entry.run_length:
                for i in range(entry.run_length):
                    tiles[entry.tile_id + i] = (header['tile_data_offset'] + entry.offset, entry.length)
            else:
                walk(header['leaf_directory_offset'] + entry.offset, entry.length)
    walk(header['root_offset'], header['root_length'])
    return tiles


def quadrants(tiles, zoom, target):
    """{(z, x, y): [tile ids]}: a quadrant splits in four while its tiles hold more than target bytes, a tile the
    archive stores once for several places counting once, as in the cell."""
    cut, stack = {}, [(0, 0, 0, sorted(tiles))]
    while stack:
        z, x, y, group = stack.pop()
        if z < zoom and sum(length for _, length in {tiles[t] for t in group}) > target:
            parts = {}
            for t in group:
                _, tx, ty = tileid_to_zxy(t)
                shift = zoom - z - 1
                parts.setdefault(((tx >> shift) & 1, (ty >> shift) & 1), []).append(t)
            stack += [(z + 1, 2 * x + dx, 2 * y + dy, part) for (dx, dy), part in parts.items()]
        else:
            cut[(z, x, y)] = group
    return cut


def latitude(row, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / 2 ** z))))


def bounds(z, x, y):
    """A quadrant's west, south, east and north, in 1e-7 degrees."""
    return [round(v * 1e7) for v in (x / 2 ** z * 360 - 180, latitude(y + 1, z), (x + 1) / 2 ** z * 360 - 180,
                                     latitude(y, z))]


@contextmanager
def archive(path):
    """The archive's header, its metadata, its tiles' places and a reader of its bytes."""
    with open(path, 'rb') as stream:
        get_bytes = MmapSource(stream)
        reader = Reader(get_bytes)
        header = reader.header()
        require(header['min_zoom'] == header['max_zoom'] and (header['tile_type'], header['tile_compression']) in KINDS,
                f'{path} is not a one-zoom archive of gzipped JSON or MVT, or of WebP')
        yield header, reader.metadata(), directory(get_bytes, header), get_bytes


def cell(get_bytes, tiles, quadrant, metadata, kind):
    """One cell's bytes: its tiles under a header and metadata that depend only on its quadrant and the tiles' kind,
    (type, compression)."""
    out = io.BytesIO()
    writer = Writer(out)
    for tile in sorted(tiles):
        writer.write_tile(tile, get_bytes(*tiles[tile]))
    west, south, east, north = bounds(*quadrant)
    writer.finalize({'tile_type': kind[0], 'tile_compression': kind[1],
                     'min_lon_e7': west, 'min_lat_e7': south, 'max_lon_e7': east, 'max_lat_e7': north,
                     'center_zoom': quadrant[0], 'center_lon_e7': (west + east) // 2,
                     'center_lat_e7': (south + north) // 2}, metadata)
    return out.getvalue()


def cut(header, metadata, tiles, get_bytes, target):
    """Each cell of an opened archive in index order, (quadrant, bytes), each read back tile for tile."""
    # The writer gzips its directories and metadata: with a timestamp in them, every cell would change every run
    require(gzip.compress(b'')[4:8] == bytes(4), 'Cells need gzip without timestamps, as in Python 3.14')
    kept = {key: metadata[key] for key in KEPT if key in metadata}
    for quadrant, group in sorted(quadrants(tiles, header['min_zoom'], target or TARGET).items()):
        data = cell(get_bytes, {t: tiles[t] for t in group}, quadrant, kept, (header['tile_type'], header['tile_compression']))
        back = Reader(MemorySource(data))
        require(all(back.get(*tileid_to_zxy(t)) == get_bytes(*tiles[t]) for t in group)
                and back.header()['addressed_tiles_count'] == len(group), f'Cell {quadrant} differs from its archive')
        yield quadrant, data


def cells(path, target=None):
    """Each cell of the archive in index order, (quadrant, bytes)."""
    with archive(path) as opened:
        yield from cut(*opened, target)


def index(path, target=None):
    """The archive's index: its zoom, and each cell as [z, x, y, bytes, sha256]; the cells lie beside it, as
    <sha256>.pmtiles."""
    with archive(path) as opened:
        rows = [[*quadrant, len(data), hashlib.sha256(data).hexdigest()] for quadrant, data in cut(*opened, target)]
    return {'schema': 1, 'zoom': opened[0]['min_zoom'], 'cells': rows}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
