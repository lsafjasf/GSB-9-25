"""Self-tests: edge cases, corruption case suite, determinism, ratios.

Run with:  python3 test_huffman.py   (or: python3 -m unittest test_huffman)
"""

import random
import struct
import unittest

import huffman
from crosscheck import crosscheck, reference_decode
from huffman import (
    BadMagicError, DataMismatchError, InvalidCodeTableError,
    TrailingBitsError, TruncatedStreamError,
)

HEADER_SIZE = 22  # 4 magic + 8 checksum + 8 length + 2 count


def corrupt(container, offset, new_byte):
    buf = bytearray(container)
    buf[offset] = new_byte
    return bytes(buf)


class EdgeCaseTests(unittest.TestCase):
    def roundtrip(self, data):
        container = huffman.encode(data)
        self.assertEqual(huffman.decode(container), data)
        self.assertEqual(reference_decode(container)[0], data)
        return container

    def test_empty(self):
        c = self.roundtrip(b"")
        self.assertEqual(len(c), HEADER_SIZE)  # header only, no table/payload

    def test_single_byte(self):
        self.roundtrip(b"Q")

    def test_single_symbol_alphabet(self):
        c = self.roundtrip(b"\xAA" * 1000)
        # 1 bit per symbol -> 125 payload bytes + 22 header + 2 table.
        self.assertEqual(len(c), HEADER_SIZE + 2 + 125)

    def test_high_bytes(self):
        self.roundtrip(bytes(range(0x80, 0x100)))
        self.roundtrip(b"\xff\xfe\x80\x90\xab\xcd\xef" * 50)

    def test_all_same_byte(self):
        self.roundtrip(b"\x00" * 65536)

    def test_all_256_symbols(self):
        self.roundtrip(bytes(range(256)) * 3)

    def test_two_symbols(self):
        self.roundtrip(b"ab")
        self.roundtrip(b"ab" * 5000)

    def test_skewed_frequencies(self):
        rng = random.Random(7)
        data = bytes(rng.choices(range(8), weights=[1000, 500, 250, 125,
                                                    60, 30, 15, 8], k=50000))
        self.roundtrip(data)

    def test_determinism(self):
        rng = random.Random(1234)
        for _ in range(20):
            data = rng.randbytes(rng.randrange(0, 3000))
            self.assertEqual(huffman.encode(data), huffman.encode(data))

    def test_determinism_tie_breaking(self):
        # Many equal frequencies: merge order must still be fixed.
        data = bytes(range(256)) * 10  # every symbol exactly 10 times
        c1 = huffman.encode(data)
        c2 = huffman.encode(bytes(bytearray(reversed(data[:256])) * 10))
        # Same frequency table -> identical code table and identical size.
        # (Checksums differ because the payloads differ; compare the
        # length/count fields and the code-table region only.)
        self.assertEqual(len(c1), len(c2))
        self.assertEqual(c1[12:HEADER_SIZE + 2 * 256],
                         c2[12:HEADER_SIZE + 2 * 256])


class CorruptionTests(unittest.TestCase):
    """Corruption case suite: every case must raise a distinguishable
    error and must never yield partial output as success."""

    @classmethod
    def setUpClass(cls):
        cls.data = (b"the quick brown fox! " * 40) + bytes(range(256))
        cls.container = huffman.encode(cls.data)
        cls.count = struct.unpack_from(">H", cls.container, 20)[0]
        cls.table_end = HEADER_SIZE + 2 * cls.count

    def test_truncated_every_prefix(self):
        # Cutting the container at ANY point must fail (truncation or,
        # for header-only cuts, a header error) -- never silent success.
        for cut in range(0, len(self.container), 7):
            with self.assertRaises((TruncatedStreamError, BadMagicError,
                                    InvalidCodeTableError),
                                   msg="cut at %d" % cut):
                huffman.decode(self.container[:cut])

    def test_truncated_bitstream(self):
        with self.assertRaises(TruncatedStreamError):
            huffman.decode(self.container[:-1])
        with self.assertRaises(TruncatedStreamError):
            huffman.decode(self.container[:self.table_end + 3])

    def test_truncated_header_and_table(self):
        with self.assertRaises(TruncatedStreamError):
            huffman.decode(self.container[:10])
        with self.assertRaises(TruncatedStreamError):
            huffman.decode(self.container[:HEADER_SIZE + 4])

    def test_bad_magic(self):
        with self.assertRaises(BadMagicError):
            huffman.decode(corrupt(self.container, 0, ord("X")))

    def test_invalid_table_kraft(self):
        # Change one code length -> Kraft sum broken.
        off = HEADER_SIZE + 1  # length byte of first entry
        bad = corrupt(self.container, off, self.container[off] + 1)
        with self.assertRaises(InvalidCodeTableError):
            huffman.decode(bad)

    def test_invalid_table_zero_length(self):
        bad = corrupt(self.container, HEADER_SIZE + 1, 0)
        with self.assertRaises(InvalidCodeTableError):
            huffman.decode(bad)

    def test_invalid_table_huge_length(self):
        bad = corrupt(self.container, HEADER_SIZE + 1, 200)
        with self.assertRaises(InvalidCodeTableError):
            huffman.decode(bad)

    def test_invalid_table_duplicate_symbol(self):
        # Force two entries to share a symbol (breaks strict ordering).
        bad = corrupt(self.container, HEADER_SIZE + 2,
                      self.container[HEADER_SIZE])
        with self.assertRaises(InvalidCodeTableError):
            huffman.decode(bad)

    def test_invalid_single_symbol_table(self):
        c = bytearray(huffman.encode(b"\x55" * 100))
        c[HEADER_SIZE + 1] = 5  # single symbol must use a 1-bit code
        with self.assertRaises(InvalidCodeTableError):
            huffman.decode(bytes(c))

    def test_trailing_extra_bytes(self):
        with self.assertRaises(TrailingBitsError):
            huffman.decode(self.container + b"\x00")
        with self.assertRaises(TrailingBitsError):
            huffman.decode(self.container + b"\xff" * 4)

    def test_trailing_nonzero_padding(self):
        # b"ab" -> codes 0,1 -> bits "01" -> byte 0b01000000, 6 pad bits.
        c = huffman.encode(b"ab")
        self.assertEqual(c[-1], 0b01000000)
        with self.assertRaises(TrailingBitsError):
            huffman.decode(c[:-1] + bytes([0b01000001]))
        with self.assertRaises(TrailingBitsError):
            huffman.decode(c[:-1] + bytes([0b01001000]))

    def test_table_data_mismatch(self):
        # Swap the code lengths of two symbols with different lengths:
        # table stays a valid prefix code, but decodes to wrong bytes.
        c = huffman.encode(b"a" * 100 + b"b" * 50 + b"c" * 25 + b"d" * 10)
        count = struct.unpack_from(">H", c, 20)[0]
        entries = {}
        for i in range(count):
            sym = c[HEADER_SIZE + 2 * i]
            entries[sym] = (HEADER_SIZE + 2 * i + 1, c[HEADER_SIZE + 2 * i + 1])
        la = entries[ord("a")]
        ld = entries[ord("d")]
        self.assertNotEqual(la[1], ld[1])
        buf = bytearray(c)
        buf[la[0]] = ld[1]
        buf[ld[0]] = la[1]
        with self.assertRaises(DataMismatchError):
            huffman.decode(bytes(buf))

    def test_bitstream_bitflip_detected(self):
        # Flipping a payload bit must be caught (checksum) -- or, if the
        # flip changes symbol boundaries, by truncation/trailing checks.
        mid = self.table_end + (len(self.container) - self.table_end) // 2
        bad = corrupt(self.container, mid, self.container[mid] ^ 0x40)
        with self.assertRaises((DataMismatchError, TruncatedStreamError,
                                TrailingBitsError)):
            huffman.decode(bad)

    def test_no_partial_output(self):
        # decode() must raise, never return truncated data.
        for cut in (len(self.container) - 1, len(self.container) - 5):
            try:
                huffman.decode(self.container[:cut])
            except (TruncatedStreamError, DataMismatchError):
                pass
            else:
                self.fail("truncated stream decoded without error")


class RatioTests(unittest.TestCase):
    def test_ratio_vs_fixed_length(self):
        rng = random.Random(99)
        text = (b"the quick brown fox jumps over the lazy dog. " * 2000)
        uniform = bytes(range(256)) * 400
        random_data = rng.randbytes(100000)
        for name, data in (("text", text), ("uniform", uniform),
                           ("random", random_data)):
            c = huffman.encode(data)
            ratio = len(c) / len(data)
            fixed = 1.0  # fixed-length 8 bits/byte baseline
            print("%-8s raw=%7d  huffman=%7d  ratio=%.3f  (fixed=1.000)"
                  % (name, len(data), len(c), ratio))
            self.assertLess(ratio, fixed) if name == "text" else None
        # Highly compressible text must beat fixed-length clearly.
        self.assertLess(len(huffman.encode(text)) / len(text), 0.6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
