"""Self-tests for the pure-python BMP library (stdlib unittest only)."""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bmp
from bmp import BMPError, BMPImage


def make_pixels(width, height, bpp, seed=7):
    """Deterministic pseudo-random pixel bytes (covers reserved channel)."""
    n = width * height * (bpp // 8)
    state = seed
    out = bytearray(n)
    for i in range(n):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = (state >> 8) & 0xFF
    return out


def make_image(width, height, bpp, top_down=False, seed=7):
    return BMPImage(width, height, bpp, make_pixels(width, height, bpp, seed), top_down)


class RoundTripTests(unittest.TestCase):
    SIZES = [(1, 1), (1, 7), (2, 5), (3, 3), (4, 4), (5, 2), (17, 1), (1, 17), (16, 16), (31, 3)]

    def test_roundtrip_pixel_and_byte_identical(self):
        for bpp in (24, 32):
            for top_down in (False, True):
                for w, h in self.SIZES:
                    with self.subTest(bpp=bpp, top_down=top_down, size=(w, h)):
                        img = make_image(w, h, bpp, top_down, seed=w * 100 + h)
                        blob = bmp.dumps(img)
                        back = bmp.loads(blob)
                        self.assertEqual(back.width, w)
                        self.assertEqual(back.height, h)
                        self.assertEqual(back.bpp, bpp)
                        self.assertEqual(back.top_down, top_down)
                        self.assertEqual(bytes(back.pixels), bytes(img.pixels))
                        # Serialising the decoded image reproduces the file.
                        self.assertEqual(bmp.dumps(back), blob)

    def test_reserved_channel_preserved_32bpp(self):
        img = make_image(9, 5, 32, seed=99)
        # Force a recognisable pattern into the 4th (reserved) byte.
        for i in range(3, len(img.pixels), 4):
            img.pixels[i] = (i * 13 + 1) & 0xFF
        expected = bytes(img.pixels)
        back = bmp.loads(bmp.dumps(img))
        self.assertEqual(bytes(back.pixels), expected)

    def test_row_padding_layout(self):
        # width=1 @24bpp -> 3 data bytes + 1 pad byte per row.
        img = make_image(1, 3, 24)
        blob = bmp.dumps(img)
        self.assertEqual(len(blob), 54 + 4 * 3)
        self.assertEqual(blob[54 + 3], 0)  # padding emitted as zero
        # width=2 @24bpp -> 6 data + 2 pad; width=4 -> no padding.
        self.assertEqual(len(bmp.dumps(make_image(2, 1, 24))), 54 + 8)
        self.assertEqual(len(bmp.dumps(make_image(4, 1, 24))), 54 + 12)
        # 32bpp rows are naturally aligned.
        self.assertEqual(len(bmp.dumps(make_image(3, 2, 32))), 54 + 24)

    def test_row_order_on_disk(self):
        img = make_image(2, 2, 24, top_down=False)
        blob = bmp.dumps(img)
        (height_field,) = struct.unpack_from("<i", blob, 22)
        self.assertEqual(height_field, 2)
        # Bottom-up: first stored row is the last logical row.
        row_bytes = 2 * 3
        last_logical = bytes(img.pixels[row_bytes : 2 * row_bytes])
        self.assertEqual(blob[54 : 54 + row_bytes], last_logical[:row_bytes])

        img_td = make_image(2, 2, 24, top_down=True)
        blob_td = bmp.dumps(img_td)
        (height_field,) = struct.unpack_from("<i", blob_td, 22)
        self.assertEqual(height_field, -2)
        first_logical = bytes(img_td.pixels[:row_bytes])
        self.assertEqual(blob_td[54 : 54 + row_bytes], first_logical)


class CorruptInputTests(unittest.TestCase):
    def corrupt(self, blob, offset, fmt, *values):
        data = bytearray(blob)
        struct.pack_into(fmt, data, offset, *values)
        return bytes(data)

    def test_bad_magic(self):
        blob = bytearray(bmp.dumps(make_image(2, 2, 24)))
        blob[0:2] = b"ZZ"
        with self.assertRaisesRegex(BMPError, "魔数"):
            bmp.loads(bytes(blob))

    def test_truncated_header(self):
        blob = bmp.dumps(make_image(2, 2, 24))
        with self.assertRaisesRegex(BMPError, "文件头非法"):
            bmp.loads(blob[:10])
        with self.assertRaisesRegex(BMPError, "文件头非法"):
            bmp.loads(b"")

    def test_bad_planes_and_header_size(self):
        blob = bmp.dumps(make_image(2, 2, 24))
        with self.assertRaisesRegex(BMPError, "planes"):
            bmp.loads(self.corrupt(blob, 26, "<H", 2))
        with self.assertRaisesRegex(BMPError, "DIB 头大小"):
            bmp.loads(self.corrupt(blob, 14, "<I", 12))

    def test_unsupported_bpp(self):
        blob = bmp.dumps(make_image(2, 2, 24))
        for depth in (1, 4, 8, 16):
            with self.assertRaisesRegex(BMPError, "位深不支持"):
                bmp.loads(self.corrupt(blob, 28, "<H", depth))

    def test_unsupported_compression(self):
        blob = bmp.dumps(make_image(2, 2, 24))
        with self.assertRaisesRegex(BMPError, "压缩方式不支持"):
            bmp.loads(self.corrupt(blob, 30, "<I", 1))

    def test_truncated_pixel_data(self):
        blob = bmp.dumps(make_image(8, 8, 24))
        with self.assertRaisesRegex(BMPError, "像素数据不足"):
            bmp.loads(blob[:-4])
        with self.assertRaisesRegex(BMPError, "像素数据不足"):
            bmp.loads(blob[:54])

    def test_size_field_mismatch(self):
        blob = bmp.dumps(make_image(4, 4, 24))
        # biSizeImage disagrees with width*height*bpp.
        with self.assertRaisesRegex(BMPError, "尺寸与数据长度不符"):
            bmp.loads(self.corrupt(blob, 34, "<I", 12))
        # Declared file size smaller than required pixel data.
        with self.assertRaisesRegex(BMPError, "尺寸与数据长度不符"):
            bmp.loads(self.corrupt(blob, 2, "<I", 60))

    def test_zero_and_negative_dimensions(self):
        blob = bmp.dumps(make_image(4, 4, 24))
        with self.assertRaisesRegex(BMPError, "宽度"):
            bmp.loads(self.corrupt(blob, 18, "<i", 0))
        with self.assertRaisesRegex(BMPError, "宽度"):
            bmp.loads(self.corrupt(blob, 18, "<i", -3))
        with self.assertRaisesRegex(BMPError, "高度为 0"):
            bmp.loads(self.corrupt(blob, 22, "<i", 0))

    def test_oversized_dimensions_rejected_with_limit(self):
        blob = bmp.dumps(make_image(4, 4, 24))
        with self.assertRaisesRegex(BMPError, "单边上限"):
            bmp.loads(self.corrupt(blob, 18, "<i", bmp.MAX_DIMENSION + 1))
        # 20000 x 20000 fits per-side limit but exceeds total pixel cap.
        data = self.corrupt(blob, 18, "<i", 20000)
        data = self.corrupt(data, 22, "<i", 20000)
        data = self.corrupt(data, 34, "<I", 0)  # biSizeImage=0 means "unset"
        data = self.corrupt(data, 2, "<I", 0)
        with self.assertRaisesRegex(BMPError, "像素总数超限"):
            bmp.loads(data)

    def test_bad_pixel_offset(self):
        blob = bmp.dumps(make_image(2, 2, 24))
        with self.assertRaisesRegex(BMPError, "像素数据偏移"):
            bmp.loads(self.corrupt(blob, 10, "<I", 20))


class ResizeTests(unittest.TestCase):
    def test_scale_one_is_identity(self):
        for bpp in (24, 32):
            img = make_image(13, 9, bpp, seed=bpp)
            self.assertEqual(bytes(bmp.resize_nearest(img, 13, 9).pixels), bytes(img.pixels))
            self.assertEqual(bytes(bmp.resize_bilinear(img, 13, 9).pixels), bytes(img.pixels))

    def test_output_geometry_and_channels(self):
        for bpp in (24, 32):
            img = make_image(1, 64, bpp)
            for fn in (bmp.resize_nearest, bmp.resize_bilinear):
                out = fn(img, 64, 1)
                self.assertEqual((out.width, out.height, out.bpp), (64, 1, bpp))
                self.assertEqual(len(out.pixels), 64 * 1 * (bpp // 8))

    def test_nearest_known_values(self):
        # 2x1 image, pixels A and B; upscale to 4x1 -> A A B B.
        img = BMPImage(2, 1, 24, bytearray([1, 2, 3, 4, 5, 6]))
        out = bmp.resize_nearest(img, 4, 1)
        self.assertEqual(list(out.pixels), [1, 2, 3, 1, 2, 3, 4, 5, 6, 4, 5, 6])
        # Downscale 4x1 -> 2x1 keeps pixels 0 and 2.
        img4 = BMPImage(4, 1, 24, bytearray(range(12)))
        out = bmp.resize_nearest(img4, 2, 1)
        self.assertEqual(list(out.pixels), [0, 1, 2, 6, 7, 8])

    def test_bilinear_known_values(self):
        # 2x1, channel values 0 and 100 -> 4x1 gives 0, 25, 75, 100
        # (centre-aligned mapping, border clamping).
        img = BMPImage(2, 1, 24, bytearray([0, 0, 0, 100, 100, 100]))
        out = bmp.resize_bilinear(img, 4, 1)
        blues = list(out.pixels[0::3])
        self.assertEqual(blues, [0, 25, 75, 100])

    def test_bilinear_interpolates_reserved_channel(self):
        img = BMPImage(2, 1, 32, bytearray([0, 0, 0, 0, 0, 0, 0, 200]))
        out = bmp.resize_bilinear(img, 4, 1)
        reserved = list(out.pixels[3::4])
        self.assertEqual(reserved, [0, 50, 150, 200])

    def test_nearest_preserves_reserved_channel(self):
        img = make_image(3, 3, 32, seed=5)
        out = bmp.resize_nearest(img, 9, 9)
        for y in range(9):
            for x in range(9):
                s = ((y // 3) * 3 + (x // 3)) * 4
                d = (y * 9 + x) * 4
                self.assertEqual(out.pixels[d : d + 4], img.pixels[s : s + 4])

    def test_extreme_aspect_ratios(self):
        wide = make_image(256, 1, 24)
        tall = make_image(1, 256, 32)
        self.assertEqual((bmp.resize_bilinear(wide, 1, 1).width), 1)
        out = bmp.resize_nearest(tall, 1, 1)
        self.assertEqual((out.width, out.height, out.bpp), (1, 1, 32))

    def test_resize_rejects_bad_target(self):
        img = make_image(4, 4, 24)
        for fn in (bmp.resize_nearest, bmp.resize_bilinear):
            with self.assertRaisesRegex(BMPError, "尺寸为零"):
                fn(img, 0, 4)
            with self.assertRaisesRegex(BMPError, "尺寸为零"):
                fn(img, 4, -1)
            with self.assertRaisesRegex(BMPError, "单边上限"):
                fn(img, bmp.MAX_DIMENSION + 1, 4)

    def test_roundtrip_after_resize(self):
        img = make_image(10, 6, 32, top_down=True, seed=3)
        out = bmp.resize_bilinear(img, 23, 17)
        blob = bmp.dumps(out)
        back = bmp.loads(blob)
        self.assertEqual(bytes(back.pixels), bytes(out.pixels))
        self.assertTrue(back.top_down)


class SampleFilesTests(unittest.TestCase):
    """The shipped sample images must parse and round-trip."""

    SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")

    def test_samples_roundtrip(self):
        if not os.path.isdir(self.SAMPLES):
            self.skipTest("samples/ not generated yet (run make_samples.py)")
        for name in sorted(os.listdir(self.SAMPLES)):
            if not name.endswith(".bmp"):
                continue
            with self.subTest(sample=name):
                path = os.path.join(self.SAMPLES, name)
                with open(path, "rb") as fh:
                    raw = fh.read()
                self.assertEqual(bmp.dumps(bmp.load(path)), raw)


if __name__ == "__main__":
    unittest.main()
