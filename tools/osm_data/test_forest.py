"""The forest's kind: the mixed rule, the smoothing, the tiles' PNG and their coarser zooms, on small synthetic rasters."""

import io
import unittest

import numpy as np
import rasterio
from rasterio.io import MemoryFile

from tools.osm_data import forest
from tools.osm_data.forest import CONIFEROUS, DECIDUOUS, MIXED, NONE, OUTSIDE, SIZE


def decoded(data):
    with MemoryFile(data) as memory, memory.open() as image:
        return image.read(1), image.colormap(1)


class ForestTests(unittest.TestCase):
    def test_box_sum_counts_the_square_around_each_pixel(self):
        a = np.zeros((7, 9), bool)
        a[3, 4] = a[0, 0] = True
        got = forest.box_sum(a, 1)
        self.assertEqual(got.shape, a.shape)
        self.assertEqual(got[2:5, 3:6].tolist(), [[1, 1, 1]] * 3)
        self.assertEqual((got[0, 0], got[1, 1], got[2, 2], got[6, 8]), (1, 1, 0, 0))
        self.assertTrue((forest.box_sum(np.ones((5, 5), bool), 2)[2, 2] == 25))

    def test_mixed_where_neither_kind_has_the_line(self):
        raw = np.full((20, 30), forest.CONIFER, np.uint8)
        raw[:, 10:20] = np.tile([forest.BROADLEAVED, forest.CONIFER], (20, 5))  # half and half
        raw[:, 20:] = forest.BROADLEAVED
        raw[0, 0], raw[19, 29] = 0, OUTSIDE
        got = forest.kinds(raw, radius=20, line=75, res=10)
        self.assertEqual((got[10, 3], got[10, 15], got[10, 26]), (CONIFEROUS, MIXED, DECIDUOUS))
        self.assertEqual((got[0, 0], got[19, 29]), (NONE, OUTSIDE))
        self.assertEqual(forest.kinds(raw, radius=0, line=75, res=10)[10, 14], DECIDUOUS)  # a pixel is its own kind

    def test_smoothing_merges_patches_under_half_a_hectare(self):
        classes = np.full((40, 40), CONIFEROUS, np.uint8)
        classes[5:9, 5:9] = DECIDUOUS  # 16 pixels of 100 m²: 0.16 ha
        classes[20:30, 20:30] = MIXED  # 1 ha
        classes[:, 38:] = OUTSIDE
        got = forest.smooth(classes, 10)
        self.assertEqual(forest.half_hectare(10), 50)
        self.assertEqual(forest.half_hectare(20), 13)
        self.assertEqual((got[6, 6], got[25, 25], got[0, 39]), (CONIFEROUS, MIXED, OUTSIDE))
        self.assertTrue((got[:, 38:] == OUTSIDE).all())

    def test_png_is_two_bit_with_the_palette_at_a_fixed_place(self):
        classes = np.random.default_rng(1).integers(0, 4, (SIZE, SIZE), dtype=np.uint8)
        data = forest.png(classes)
        self.assertEqual(data[12:16], b'IHDR')
        self.assertEqual(data[24:26], bytes([2, 3]))  # bit depth 2, palette
        self.assertEqual(data[33:41], b'\x00\x00\x00\x0cPLTE')
        self.assertEqual(data[41:53], forest.PALETTE)
        self.assertEqual(data[57:66], b'\x00\x00\x00\x01tRNS\x00')
        pixels, colours = decoded(data)
        self.assertEqual(pixels.tolist(), classes.tolist())
        self.assertEqual(colours[1][:3], tuple(forest.PALETTE[3:6]))
        self.assertEqual(colours[0][3], 0)  # no forest is transparent
        self.assertEqual(forest.png(classes), data)

    def test_coarser_takes_the_commonest_kind_and_keeps_outside_only_where_all_is(self):
        children = [np.full((SIZE, SIZE), OUTSIDE, np.uint8) for _ in range(4)]
        children[0][:] = CONIFEROUS
        children[0][0, 1] = children[0][1, 0] = DECIDUOUS  # a tie: the lower kind
        children[0][2:4, 2:4] = [[MIXED, MIXED], [MIXED, NONE]]
        children[1][:2, :2] = [[OUTSIDE, OUTSIDE], [OUTSIDE, DECIDUOUS]]
        got = forest.coarser(children)
        self.assertEqual((got[0, 0], got[1, 1], got[0, 256], got[0, 257], got[300, 0]),
                         (CONIFEROUS, MIXED, DECIDUOUS, OUTSIDE, OUTSIDE))

    def test_tile_transform_is_the_tiles_place_in_web_mercator(self):
        t = forest.tile_transform(12, 2315, 1144)
        self.assertAlmostEqual(t.a, 2 * 20037508.342789244 / 4096 / SIZE)
        self.assertAlmostEqual(t.c, -20037508.342789244 + 2315 * 2 * 20037508.342789244 / 4096)


if __name__ == '__main__':
    unittest.main()
