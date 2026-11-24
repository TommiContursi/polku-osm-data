"""A box of a remote PMTiles archive, one zoom at a time, read by HTTP Range: the directory leaves its tiles lie in,
then the tiles themselves in runs of up to 32 MiB, one request at a time. Each zoom becomes a one-zoom archive of its
own, its tiles byte for byte the source's."""

from collections import Counter
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import re
import time
import urllib.request
from urllib.error import HTTPError

from pmtiles.reader import MmapSource, all_tiles
from pmtiles.tile import deserialize_directory, deserialize_header, find_tile, zxy_to_tileid
from pmtiles.writer import Writer

from tools.osm_data import mml
from tools.osm_data.publish import require
from tools.osm_data.routing import stamp

RUN = 32 * 1024**2  # bytes a request at most
GAP = 256 * 1024  # bytes between two tiles read through rather than asked for in a request of their own
ATTEMPTS, BACK_OFF = 3, 60  # tries a request, and seconds before the next


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
                    require(self.version in (None, version), f'{self.url} changed during the extract')
                    self.version = version
                    body = response.read()
                    require(len(body) == length, f'{self.url}: {len(body)} of {length} bytes')
                    return body
            except (OSError, http.client.HTTPException) as error:  # HTTP errors and bodies cut short too
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
        while (entry := find_tile(directory, tile)) is not None:
            if entry.run_length:
                found[tile] = (h['tile_data_offset'] + entry.offset, entry.length)
                break
            key = h['leaf_directory_offset'] + entry.offset
            if key not in leaves:
                leaves[key] = deserialize_directory(remote.read(key, entry.length))
            directory = leaves[key]
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
