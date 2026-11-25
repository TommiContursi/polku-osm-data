#!/usr/bin/env python3
"""The forest's kind (Polku #1360): coniferous, mixed and deciduous forest as 512 px class tiles at zooms 10-12, one
PMTiles archive a zoom, uploaded in cells among the shared files as a candidate in polku-forest. Finland's kinds are
Syke's CORINE Land Cover 2024; Sweden's, Norway's and Estonia's are the Copernicus Land Monitoring Service's Forest
Type 2024, read from the Copernicus Data Space, with mixed forest found by a rule tuned on Finland against Syke's.

By hand, when a source publishes a new edition:
  fetch COUNTRY OUT.tif   a country's kinds, smoothed to 0.5 ha, in the source's grid (255 outside the country)
  build RELEASE           the tiles from the countries' rasters in forest-sources/, then the manifest
  tune                    Forest Type's mixed rule against Syke's kinds in Finland, for RADIUS and LINE
  check                   the Data Space keys open a Forest Type square
and the copies' install-hosting, upload, candidate, promote and restore (extract.py)."""

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from multiprocessing import Pool
import os
from pathlib import Path
import shutil
import struct
import sys
import urllib.request
import zipfile
import zlib

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.errors import RasterioIOError
from rasterio.features import rasterize, sieve
from rasterio.merge import merge
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds, transform_geom
from rasterio.windows import Window, from_bounds
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from tools.osm_data import extract, mml
from tools.osm_data.publish import require
from tools.osm_data.routing import stamp

DATA = Path(__file__).resolve().parents[2] / 'data'
COUNTRIES = ('finland', 'sweden', 'norway', 'estonia')  # in this order a pixel takes the first country that has it
SOURCES = Path('forest-sources')  # where build finds each fetched country, <country>.tif and <country>.json
FOLDER = 'polku-forest'
BUDGET = 2 * 1024**3  # 0.81 GB, and a new edition beside it while it is a candidate
ZOOMS = (10, 11, 12)
SIZE = 512
NONE, CONIFEROUS, MIXED, DECIDUOUS, OUTSIDE = 0, 1, 2, 3, 255
HECTARE_HALF = 5000  # m²: no patch of one kind is smaller (sieve)
HALO = 128  # pixels read around each window, for the mixed rule's neighbourhood and the sieve

SYKE = 'https://wwwd3.ymparisto.fi/d3/Static_rs/spesific/Clc2024_FI20m.zip'
SYKE_TIF = 'Clc2024_FI20m.tif'
SYKE_KINDS = {CONIFEROUS: (3121, 3122, 3123), MIXED: (3131, 3132, 3133), DECIDUOUS: (3111, 3112)}  # level-4 codes
SYKE_NODATA = 65535

STAC = 'https://stac.dataspace.copernicus.eu/v1/search'
COLLECTION = 'clms_vlcc_forest-type_europe_10m_3yearly_v1'
YEAR = 2024
ENDPOINT = 'eodata.dataspace.copernicus.eu'
SQUARE = 100_000  # m: Forest Type's files are 100 km squares of EPSG:3035
BROADLEAVED, CONIFER = 1, 2  # Forest Type's values; 0 is no forest, 255 outside its area
RADIUS, LINE = 30, 85  # m and %: mixed unless one kind has LINE % of the forest within RADIUS (tune, 2026-11-25)

PALETTE = bytes.fromhex('000000' 'bad4b4' 'd1e2be' 'e9f0c2')  # Trail's greens; Polku swaps them for each map
CREDIT = '© Syke, © EU, Copernicus Land Monitoring Service'
LICENSE = '''The forest's kind, made by polku-osm-data from two sources, both modified: their forest classed into
coniferous, mixed and deciduous, patches under half a hectare merged into their neighbours, and cut into tiles.

Finland: CORINE Land Cover 2024 (CORINE maanpeite 2024), 20 m, by the Finnish Environment Institute (Syke),
{syke}, last modified {syke_modified}. Licence: CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).

Sweden, Norway and Estonia: High Resolution Layer Forest Type 2024, 10 m (Copernicus Land Monitoring Service,
{collection}). © European Union, Copernicus Land Monitoring Service {year}, European Environment Agency (EEA).
Free, full and open access under the Copernicus data policy (Regulation (EU) No 1159/2013); the source must be named.
Mixed forest is this build's: where neither kind has {line} % of the forest within {radius} m.
'''


def tile_transform(z, x, y):
    """The affine transform of tile z/x/y's SIZE × SIZE pixels in EPSG:3857."""
    world = 2 * 20037508.342789244
    res = world / 2 ** z / SIZE
    return from_origin(-world / 2 + x * SIZE * res, world / 2 - y * SIZE * res, res, res)


def half_hectare(res):
    """The sieve's size: pixels of res metres in half a hectare."""
    return -(-HECTARE_HALF // round(res * res))


def box_sum(a, r):
    """Each pixel's count of true pixels within r pixels (a square of side 2r + 1), by an integral image."""
    c = np.pad(a.astype(np.int32), ((r + 1, r), (r + 1, r))).cumsum(0).cumsum(1)
    n = 2 * r + 1
    return c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]


def kinds(raw, radius=RADIUS, line=LINE, res=10):
    """Forest Type's values as the four kinds: mixed where neither broadleaved nor conifers have line % of the
    forest pixels within radius metres."""
    broad, forest = raw == BROADLEAVED, (raw == BROADLEAVED) | (raw == CONIFER)
    r = round(radius / res)
    b, f = box_sum(broad, r), box_sum(forest, r)
    out = np.where(raw == OUTSIDE, OUTSIDE, NONE).astype(np.uint8)
    out[forest] = MIXED
    out[forest & (100 * b >= line * f)] = DECIDUOUS
    out[forest & (100 * (f - b) >= line * f)] = CONIFEROUS
    return out


def smooth(classes, res):
    """No patch of a kind, or of no forest, smaller than half a hectare: each merges into its largest neighbour."""
    inside = classes != OUTSIDE
    if not inside.any():
        return classes
    return sieve(classes, half_hectare(res), mask=inside)


def syke_table():
    """Syke's level-4 codes to the kinds; every other code is no forest."""
    table = np.full(SYKE_NODATA + 1, NONE, np.uint8)
    for kind, codes in SYKE_KINDS.items():
        table[list(codes)] = kind
    table[SYKE_NODATA] = OUTSIDE
    return table


def windows(width, height, step):
    for row in range(0, height, step):
        for col in range(0, width, step):
            yield Window(col, row, min(step, width - col), min(step, height - row))


def grown(window, halo, width, height):
    """The window with halo pixels around it, inside the raster, and where the window lies in it."""
    col, row = max(0, window.col_off - halo), max(0, window.row_off - halo)
    end_col = min(width, window.col_off + window.width + halo)
    end_row = min(height, window.row_off + window.height + halo)
    inner = (slice(window.row_off - row, window.row_off - row + window.height),
             slice(window.col_off - col, window.col_off - col + window.width))
    return Window(col, row, end_col - col, end_row - row), inner


def profile(crs, transform, width, height):
    return {'driver': 'GTiff', 'dtype': 'uint8', 'count': 1, 'nodata': OUTSIDE, 'crs': crs, 'transform': transform,
            'width': width, 'height': height, 'tiled': True, 'blockxsize': 512, 'blockysize': 512,
            'compress': 'deflate', 'predictor': 1, 'sparse_ok': True, 'bigtiff': 'yes'}


def download(url, path):
    """url into path; its Last-Modified and ETag."""
    request = urllib.request.Request(url, headers={'User-Agent': mml.AGENT})
    with urllib.request.urlopen(request, timeout=300) as response, open(path, 'wb') as out:
        shutil.copyfileobj(response, out, 1 << 20)
        return stamp(parsedate_to_datetime(response.headers['Last-Modified'])), response.headers.get('ETag')


def finland(out, box=None, work=Path('syke')):
    """Syke's kinds over Finland, smoothed, in its own grid (EPSG:3067, 20 m)."""
    work.mkdir(exist_ok=True)
    modified, etag = download(SYKE, work / 'syke.zip')
    with zipfile.ZipFile(work / 'syke.zip') as z:
        z.extract(SYKE_TIF, work)
    table = syke_table()
    with rasterio.open(work / SYKE_TIF) as src:
        require(src.crs.to_epsg() == 3067 and src.res == (20.0, 20.0) and src.dtypes[0] == 'uint16'
                and src.nodata == SYKE_NODATA, f'{SYKE} is no longer 20 m uint16 in EPSG:3067')
        wanted = box and from_bounds(*transform_bounds('EPSG:4326', src.crs, *box), src.transform)
        with rasterio.open(out, 'w', **profile(src.crs, src.transform, src.width, src.height)) as dst:
            for window in windows(src.width, src.height, 4096):
                if wanted and not overlaps(window, wanted):
                    continue
                outer, inner = grown(window, HALO, src.width, src.height)
                classes = table[src.read(1, window=outer)]
                if (classes == OUTSIDE).all():
                    continue
                dst.write(smooth(classes, 20)[inner], 1, window=window)
    (work / SYKE_TIF).unlink()
    return {'url': SYKE, 'timestamp': modified, 'etag': etag}


def overlaps(a, b):
    return (a.col_off < b.col_off + b.width and b.col_off < a.col_off + a.width
            and a.row_off < b.row_off + b.height and b.row_off < a.row_off + a.height)


def s3():
    """GDAL's settings for the Data Space's S3, from the keys in CDSE_S3_ACCESS_KEY and CDSE_S3_SECRET_KEY. rasterio
    leaves AWS credentials to boto3, so GDAL reads them from the environment."""
    key, secret = os.environ.get('CDSE_S3_ACCESS_KEY'), os.environ.get('CDSE_S3_SECRET_KEY')
    require(key and secret, 'Set CDSE_S3_ACCESS_KEY and CDSE_S3_SECRET_KEY, the Copernicus Data Space keys')
    os.environ.update(AWS_ACCESS_KEY_ID=key, AWS_SECRET_ACCESS_KEY=secret, AWS_S3_ENDPOINT=ENDPOINT,
                      AWS_VIRTUAL_HOSTING='FALSE', AWS_HTTPS='YES', AWS_REGION='default')
    return rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.tif',
                        GDAL_HTTP_MAX_RETRY='4', GDAL_HTTP_RETRY_DELAY='30')


def squares(box):
    """Forest Type's YEAR squares touching box: {(east, north) in 100 km: /vsis3/ path}."""
    body = {'collections': [COLLECTION], 'bbox': list(box), 'limit': 200,
            'datetime': f'{YEAR}-01-01T00:00:00Z/{YEAR}-12-31T23:59:59Z'}
    found = {}
    while body:
        request = urllib.request.Request(STAC, json.dumps(body).encode(), {'Content-Type': 'application/json',
                                                                           'User-Agent': mml.AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:
            page = json.load(response)
        for item in page['features']:
            name = item['id']  # CLMS_HRLVLCC_FTY_S2024_R10m_E52N42_03035_V01_R00
            require(f'_S{YEAR}_R10m_' in name and '_03035_' in item['assets']['data']['href'], f'Unexpected square {name}')
            e, n = name.split('_R10m_')[1].split('_')[0][1:].split('N')
            found[int(e), int(n)] = '/vsis3/' + item['assets']['data']['href'].removeprefix('s3://')
        body = next((link.get('body') for link in page.get('links', []) if link['rel'] == 'next'), None)
    return found


def outline(country):
    """The country's coverage polygon (data/<country>-coverage.geojson), as the OSM data's."""
    return json.loads((DATA / f'{country}-coverage.geojson').read_bytes())


def copernicus(country, out, box=None):
    """Forest Type's kinds over the country, mixed by the rule, smoothed, in its grid (EPSG:3035, 10 m)."""
    shape = outline(country)
    area = rasterio.features.bounds(shape)
    if box:
        area = (max(area[0], box[0]), max(area[1], box[1]), min(area[2], box[2]), min(area[3], box[3]))
    with s3():
        found = squares(area)
        require(found, f'No Forest Type squares in {area}')
        local = transform_geom('EPSG:4326', 'EPSG:3035', shape)
        wanted = box and transform_bounds('EPSG:4326', 'EPSG:3035', *box)
        es, ns = [e for e, _ in found], [n for _, n in found]
        west, north = min(es) * SQUARE, (max(ns) + 1) * SQUARE
        width, height = (max(es) + 1 - min(es)) * SQUARE // 10, (max(ns) + 1 - min(ns)) * SQUARE // 10
        grid = from_origin(west, north, 10, 10)
        with rasterio.open(out, 'w', **profile(CRS.from_epsg(3035), grid, width, height)) as dst:
            for e, n in sorted(found):
                core = (e * SQUARE, n * SQUARE, (e + 1) * SQUARE, (n + 1) * SQUARE)
                if wanted and not (core[0] < wanted[2] and wanted[0] < core[2] and core[1] < wanted[3] and wanted[1] < core[3]):
                    continue
                pad = HALO * 10
                bounds = (core[0] - pad, core[1] - pad, core[2] + pad, core[3] + pad)
                near = [found[k] for k in ((e + i, n + j) for i in (-1, 0, 1) for j in (-1, 0, 1)) if k in found]
                raw, at = read(near, bounds)
                inside = rasterize([local], raw.shape, transform=at, fill=0, default_value=1, dtype='uint8')
                raw[inside == 0] = OUTSIDE
                if (raw == OUTSIDE).all():
                    continue
                classes = smooth(kinds(raw), 10)
                window = from_bounds(*core, grid).round_offsets().round_lengths()
                dst.write(classes[HALO:-HALO, HALO:-HALO], 1, window=window)
                print(f'{country} E{e}N{n}', flush=True)
    return {'collection': COLLECTION, 'year': YEAR, 'squares': len(found), 'radius': RADIUS, 'line': LINE}


def read(paths, bounds):
    """The squares' pixels over bounds, OUTSIDE where none has any; refused keys say so."""
    try:
        data, at = merge(paths, bounds=bounds, res=10, nodata=OUTSIDE, dtype='uint8')
    except RasterioIOError as error:
        refused(error)
    return data[0], at


def refused(error):
    """A read's error, said plainly when the Data Space refused the keys."""
    if any(word in str(error) for word in ('403', 'AccessDenied', 'Access Denied', 'InvalidAccessKeyId',
                                           'SignatureDoesNotMatch', 'ExpiredToken')):
        raise RuntimeError('The Copernicus Data Space refused the keys: they may have expired. Make new ones at '
                           'eodata-s3keysmanager.dataspace.copernicus.eu and update the secrets CDSE_S3_ACCESS_KEY '
                           'and CDSE_S3_SECRET_KEY') from None
    raise error


def fetch(country, out, box=None):
    """A country's raster for build, and beside it its source as JSON."""
    out = Path(out)
    source = finland(out, box) if country == 'finland' else copernicus(country, out, box)
    out.with_suffix('.json').write_text(json.dumps(source, sort_keys=True))


def check():
    """The keys open one square of Forest Type YEAR over Helsinki: its grid, never its pixels or the keys."""
    with s3():
        found = squares((24.9, 60.1, 25.0, 60.2))
        require(found, 'STAC lists no Forest Type square over Helsinki')
        path = next(iter(found.values()))
        try:
            with rasterio.open(path) as src:
                print(json.dumps({'square': path.rsplit('/', 1)[-1], 'crs': str(src.crs), 'res': src.res,
                                  'dtype': src.dtypes[0], 'nodata': src.nodata, 'size': [src.width, src.height]}))
                src.read(1, window=Window(0, 0, 256, 256))
        except RasterioIOError as error:
            refused(error)


def png(classes):
    """A SIZE × SIZE tile of the four kinds as a 2-bit palette PNG: PLTE right after IHDR (byte 33, 12 bytes), so a
    reader can swap the greens; index 0, no forest, transparent."""
    packed = (classes[:, 0::4] << 6) | (classes[:, 1::4] << 4) | (classes[:, 2::4] << 2) | classes[:, 3::4]
    rows = np.hstack([np.zeros((SIZE, 1), np.uint8), packed]).tobytes()  # filter 0 on each row

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', SIZE, SIZE, 2, 3, 0, 0, 0))
            + chunk(b'PLTE', PALETTE) + chunk(b'tRNS', b'\x00') + chunk(b'IDAT', zlib.compress(rows, 9))
            + chunk(b'IEND', b''))


def coarser(children):
    """A tile from its four children (top left, top right, bottom left, bottom right), each 2 × 2 pixels as the most
    common kind among them, the lower kind on a tie; OUTSIDE only where all four are."""
    full = np.block([[children[0], children[1]], [children[2], children[3]]])
    blocks = full.reshape(SIZE, 2, SIZE, 2).transpose(0, 2, 1, 3).reshape(SIZE, SIZE, 4)
    counts = np.stack([(blocks == k).sum(2) for k in (NONE, CONIFEROUS, MIXED, DECIDUOUS)])
    out = counts.argmax(0).astype(np.uint8)
    out[counts.sum(0) == 0] = OUTSIDE
    return out


OPENED = []


def opened():
    """Each fetched country's raster, open in this process, in COUNTRIES order."""
    if not OPENED:
        OPENED.extend(rasterio.open(SOURCES / f'{c}.tif') for c in COUNTRIES if (SOURCES / f'{c}.tif').exists())
    return OPENED


def use(sources):
    """A worker reads the rasters in sources (its parent's SOURCES, where processes are spawned rather than forked)."""
    global SOURCES
    SOURCES = sources


def tile(z, x, y):
    """Tile z/x/y's kinds from the countries' rasters: each pixel the first country's that has it."""
    out = np.full((SIZE, SIZE), OUTSIDE, np.uint8)
    for src in opened():
        part = np.full((SIZE, SIZE), OUTSIDE, np.uint8)
        reproject(rasterio.band(src, 1), part, src_nodata=OUTSIDE, dst_transform=tile_transform(z, x, y),
                  dst_crs='EPSG:3857', dst_nodata=OUTSIDE, resampling=Resampling.nearest)
        out = np.where(out == OUTSIDE, part, out)
    return out


def block(args):
    """A zoom-10 tile and its zoom-11 and zoom-12 tiles inside box, as {(z, x, y): PNG}; a tile wholly outside every
    country is left out."""
    x10, y10, box = args
    done, grids = {}, {}
    for dy in range(4):
        for dx in range(4):
            x, y = x10 * 4 + dx, y10 * 4 + dy
            inside = not box or (mml.column(box[0], 12) <= x <= mml.column(box[2], 12)
                                 and mml.row(box[3], 12) <= y <= mml.row(box[1], 12))
            grids[12, x, y] = tile(12, x, y) if inside else np.full((SIZE, SIZE), OUTSIDE, np.uint8)
    for z in (11, 10):
        shift = 12 - z
        for y in range(y10 << (z - 10), (y10 + 1) << (z - 10)):
            for x in range(x10 << (z - 10), (x10 + 1) << (z - 10)):
                grids[z, x, y] = coarser([grids[z + 1, 2 * x + i, 2 * y + j] for j in (0, 1) for i in (0, 1)])
    for key, classes in grids.items():
        if (classes != OUTSIDE).any():
            done[key] = png(np.where(classes == OUTSIDE, NONE, classes))
    return done


def build(output, box):
    """The release from the countries' rasters in SOURCES: an archive a zoom, the licence and the manifest."""
    output = Path(output)
    require(not output.exists(), 'The release folder exists')
    sources = {c: json.loads((SOURCES / f'{c}.json').read_text()) for c in COUNTRIES if (SOURCES / f'{c}.json').exists()}
    require('finland' in sources and len(sources) > 1, f'{SOURCES} needs Finland and Forest Type')
    output.mkdir(parents=True)
    blocks = set()
    for src in opened():
        west, south, east, north = transform_bounds(src.crs, 'EPSG:4326', *src.bounds)
        west, south, east, north = max(west, box[0]), max(south, box[1]), min(east, box[2]), min(north, box[3])
        if west < east and south < north:
            blocks.update((x, y) for x in range(mml.column(west, 10), mml.column(east, 10) + 1)
                          for y in range(mml.row(north, 10), mml.row(south, 10) + 1))
    OPENED.clear()  # each worker opens its own
    tiles = {z: {} for z in ZOOMS}
    with Pool(os.cpu_count(), initializer=use, initargs=(SOURCES,)) as pool:
        for i, done in enumerate(pool.imap_unordered(block, [(x, y, box) for x, y in sorted(blocks)]), 1):
            for (z, x, y), data in done.items():
                tiles[z][zxy_to_tileid(z, x, y)] = data
            if i % 50 == 0:
                print(f'{i} of {len(blocks)} zoom-10 blocks', flush=True)
    counts = {}
    for z in ZOOMS:
        require(tiles[z], f'No forest tiles at zoom {z} in {box}')
        with open(output / f'forest-{z}.pmtiles', 'wb') as stream:
            writer = Writer(stream)
            for tile_id in sorted(tiles[z]):
                writer.write_tile(tile_id, tiles[z][tile_id])
            writer.finalize({'tile_type': TileType.PNG, 'tile_compression': Compression.NONE,
                             'min_lon_e7': round(box[0] * 1e7), 'min_lat_e7': round(box[1] * 1e7),
                             'max_lon_e7': round(box[2] * 1e7), 'max_lat_e7': round(box[3] * 1e7)},
                            {'name': f'Forest kind, zoom {z}', 'attribution': CREDIT})
        counts[z] = len(tiles[z])
    syke = sources['finland']
    licence = LICENSE.format(syke=syke['url'], syke_modified=syke['timestamp'], collection=COLLECTION, year=YEAR,
                             line=LINE, radius=RADIUS)
    fields = {'source': sources, 'attribution': CREDIT, 'license': 'CC BY 4.0 (Syke); Copernicus data policy (EU)',
              'kinds': ['none', 'coniferous', 'mixed', 'deciduous'], 'tile_size': SIZE}
    return extract.manifest(output, 'forest', counts, {'LICENSE.txt': licence.encode()}, fields, box,
                            datetime.now(timezone.utc))


RADII, LINES = (10, 20, 30, 50, 75, 100), (60, 65, 70, 75, 80, 85, 90)
SAMPLE = 30_000  # m: the middle of a square compared


def tune(finland_tif=SOURCES / 'finland.tif', samples=12):
    """Forest Type's mixed rule against Syke's smoothed kinds (fetch finland) in the middle of sample squares across
    Finland: for each radius and line, over the pixels both call forest, the share where they agree, each kind's F1
    and their mean, and each kind's share. Printed as Markdown, best mean F1 first."""
    syke = rasterio.open(finland_tif)
    with s3():
        found = squares(transform_bounds(syke.crs, 'EPSG:4326', *syke.bounds))
        picked = []
        for e, n in sorted(found, key=lambda k: (k[1], k[0])):
            pad = (SQUARE - SAMPLE) // 2
            core = (e * SQUARE + pad, n * SQUARE + pad, (e + 1) * SQUARE - pad, (n + 1) * SQUARE - pad)
            truth = np.full((SAMPLE // 10, SAMPLE // 10), OUTSIDE, np.uint8)
            reproject(rasterio.band(syke, 1), truth, src_nodata=OUTSIDE, dst_transform=from_origin(core[0], core[3], 10, 10),
                      dst_crs='EPSG:3035', dst_nodata=OUTSIDE, resampling=Resampling.nearest)
            if (truth != OUTSIDE).mean() >= 0.9:
                picked.append(((e, n), core, truth))
        step = max(1, len(picked) // samples)
        picked = picked[::step][:samples]
        require(picked, 'No square of Finland to compare')
        raws = []
        for (e, n), core, truth in picked:
            pad = HALO * 10
            near = [found[k] for k in ((e + i, n + j) for i in (-1, 0, 1) for j in (-1, 0, 1)) if k in found]
            raws.append((read(near, (core[0] - pad, core[1] - pad, core[2] + pad, core[3] + pad))[0], truth))
    print(f"Squares: {', '.join(f'E{e}N{n}' for (e, n), _, _ in picked)}\n")
    rows = []
    for radius in RADII:
        for line in LINES:
            confusion = np.zeros((3, 3), np.int64)
            for raw, truth in raws:
                ours = smooth(kinds(raw, radius, line), 10)[HALO:-HALO, HALO:-HALO]
                both = (ours >= CONIFEROUS) & (ours <= DECIDUOUS) & (truth >= CONIFEROUS) & (truth <= DECIDUOUS)
                confusion += np.bincount((truth[both] - 1) * 3 + ours[both] - 1, minlength=9).reshape(3, 3)
            rows.append((radius, line, confusion))
    syke_share = rows[0][2].sum(1) / rows[0][2].sum()
    print('| Radius (m) | Line (%) | Agree | F1 conif. | F1 mixed | F1 decid. | Mean F1 | Conif. | Mixed | Decid. |')
    print('|---|---|---|---|---|---|---|---|---|---|')
    print(f'| Syke | | | | | | | {syke_share[0]:.0%} | {syke_share[1]:.0%} | {syke_share[2]:.0%} |')
    scored = []
    for radius, line, c in rows:
        f1 = [2 * c[k, k] / max(1, c[k].sum() + c[:, k].sum()) for k in range(3)]
        share = c.sum(0) / c.sum()
        scored.append((sum(f1) / 3, radius, line, np.trace(c) / c.sum(), f1, share))
    for mean, radius, line, agree, f1, share in sorted(scored, key=lambda s: -s[0]):
        print(f'| {radius} | {line} | {agree:.1%} | {f1[0]:.2f} | {f1[1]:.2f} | {f1[2]:.2f} | {mean:.3f} | '
              f'{share[0]:.0%} | {share[1]:.0%} | {share[2]:.0%} |')


BOX = (4.0, 54.5, 31.6, 71.3)  # west, south, east, north: the four countries' coverage
OWN = {'fetch', 'tune', 'check'}

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in OWN:
        parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        commands = parser.add_subparsers(dest='command', required=True)
        fetching = commands.add_parser('fetch')
        fetching.add_argument('country', choices=COUNTRIES)
        fetching.add_argument('output', type=Path)
        fetching.add_argument('--box', help='west,south,east,north in degrees: only the source inside it')
        commands.add_parser('tune')
        commands.add_parser('check')
        args = parser.parse_args()
        try:
            if args.command == 'fetch':
                fetch(args.country, args.output, args.box and tuple(map(float, args.box.split(','))))
            elif args.command == 'tune':
                tune()
            else:
                check()
        except Exception as error:
            parser.exit(1, f'{args.command} failed: {error}\n')
        print(f'{args.command}: done')
    else:
        extract.main(__doc__, FOLDER, BUDGET, BOX, build)
