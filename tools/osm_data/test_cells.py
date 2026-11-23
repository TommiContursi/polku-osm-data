"""The cut: cells give back the archive tile for tile, and their bytes depend only on their tiles and quadrant."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from pmtiles.reader import MemorySource, Reader, all_tiles
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from tools.osm_data import cells


def write(path, tiles, source, tile_type=TileType.UNKNOWN):
    """An archive as the builder writes one: one zoom, gzip tiles, and metadata that changes every week."""
    with open(path, 'wb') as stream:
        writer = Writer(stream)
        for (z, x, y), data in sorted(tiles.items(), key=lambda item: zxy_to_tileid(*item[0])):
            writer.write_tile(zxy_to_tileid(z, x, y), data)
        writer.finalize({'tile_type': tile_type, 'tile_compression': Compression.GZIP, 'min_lon_e7': 200000000,
                         'min_lat_e7': 600000000, 'max_lon_e7': 210000000, 'max_lat_e7': 610000000},
                        {'name': 'Polku OSM roads and paths', 'format': 'json', 'schema': 1, 'attribution': 'OSM',
                         'license': 'ODbL', 'source': source, 'bounds': [20, 60, 21, 61]})
    return path


def tile(n):
    return hashlib.sha256(str(n).encode()).digest() * (1 + n % 3)


class CellTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.tiles = {(14, 9100 + n % 30, 4600 + n // 30): tile(n) for n in range(600)}

    def test_cells_give_back_the_archive_and_change_only_where_its_tiles_do(self):
        week = write(self.tmp / 'week.pmtiles', self.tiles, {'timestamp': '2026-11-16'})
        changed = self.tiles | {(14, 9100, 4600): b'changed'}
        next_week = write(self.tmp / 'next.pmtiles', changed, {'timestamp': '2026-11-23'})
        first = cells.index(week, 4096)
        self.assertEqual((first['schema'], first['zoom']), (1, 14))
        self.assertGreater(len(first['cells']), 8)
        self.assertEqual(first, cells.index(week, 4096), 'the same archive cuts to the same bytes')
        second = cells.index(next_week, 4096)
        new = [row for row in second['cells'] if row not in first['cells']]
        self.assertEqual(len(new), 1, 'one cell changed, though the metadata did too')
        z, x, y = new[0][:3]
        self.assertEqual((9100 >> (14 - z), 4600 >> (14 - z)), (x, y), 'the one holding the changed tile')
        seen = {}
        for quadrant, data in cells.cells(next_week, 4096):
            for zxy, body in all_tiles(MemorySource(data)):
                self.assertNotIn(zxy, seen)
                seen[zxy] = body
        self.assertEqual(seen, changed)
        for row in second['cells']:
            self.assertLessEqual(row[3], 4096 + 1024, 'at most the target, beside its header and directory')

    def test_vector_tiles_are_cut_into_cells_of_vector_tiles(self):
        mvt = write(self.tmp / 'mvt.pmtiles', self.tiles, {}, TileType.MVT)
        for _, data in cells.cells(mvt, 4096):
            self.assertEqual(Reader(MemorySource(data)).header()['tile_type'], TileType.MVT)

    def test_the_cell_fixtures_are_the_cuts_of_the_geometry_and_traffic_fixtures(self):
        fixtures = Path(__file__).resolve().parents[1] / 'fixtures'
        for name in ('osm-geometry', 'digiroad-traffic'):
            with self.subTest(name):
                made = {hashlib.sha256(data).hexdigest() + '.pmtiles': data
                        for _, data in cells.cells(fixtures / f'{name}.pmtiles', 4096)}
                made['index.json'] = cells.encoded(cells.index(fixtures / f'{name}.pmtiles', 4096))
                self.assertEqual({p.name: p.read_bytes() for p in (fixtures / f'{name}-cells').iterdir()}, made)

    def test_a_tile_larger_than_the_target_is_a_cell_of_its_own(self):
        big = write(self.tmp / 'big.pmtiles', {(14, 9100, 4600): bytes(5000), (14, 9101, 4600): b'small'}, {})
        rows = cells.index(big, 4096)['cells']
        self.assertEqual([row[:3] for row in rows], [[14, 9100, 4600], [14, 9101, 4600]])


if __name__ == '__main__':
    unittest.main()
