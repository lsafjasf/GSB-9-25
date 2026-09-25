"""Edge-case and round-trip tests for huffman.py. Run: python3 test_huffman.py"""

import random
import unittest

import huffman


class RoundTripTests(unittest.TestCase):
    def roundtrip(self, data: bytes):
        blob = huffman.compress(data)
        self.assertEqual(huffman.compress(data), blob, "encoding not deterministic")
        self.assertEqual(huffman.decompress(blob), data)
        return blob

    def test_empty(self):
        blob = self.roundtrip(b"")
        self.assertEqual(huffman.decompress(blob), b"")

    def test_single_byte(self):
        self.roundtrip(b"A")
        self.roundtrip(b"\x00")
        self.roundtrip(b"\xff")

    def test_single_symbol_repeated(self):
        for n in (1, 2, 3, 7, 8, 9, 100, 10000):
            self.roundtrip(b"\xab" * n)

    def test_all_same_byte_values(self):
        for value in (0x00, 0x01, 0x7F, 0x80, 0xFE, 0xFF):
            self.roundtrip(bytes([value]) * 500)

    def test_two_symbols(self):
        self.roundtrip(b"\x00\xff" * 300)

    def test_high_bytes(self):
        self.roundtrip(bytes(range(0x80, 0x100)) * 20)

    def test_all_256_symbols(self):
        self.roundtrip(bytes(range(256)) * 40)

    def test_text(self):
        self.roundtrip(b"the quick brown fox jumps over the lazy dog. " * 200)

    def test_skewed_frequencies(self):
        rng = random.Random(42)
        data = bytes(rng.choices(range(256), weights=[1 << (i % 9) for i in range(256)], k=20000))
        self.roundtrip(data)

    def test_random_roundtrips(self):
        rng = random.Random(1234)
        for size in (1, 2, 3, 5, 17, 100, 999, 4096, 65536):
            self.roundtrip(rng.randbytes(size))

    def test_determinism_across_dict_order(self):
        # Same multiset of bytes, different byte orderings must still each
        # encode deterministically (compress is a pure function of input).
        rng = random.Random(7)
        data = bytearray(rng.randbytes(5000))
        self.assertEqual(huffman.compress(bytes(data)), huffman.compress(bytes(data)))
        rng.shuffle(data)
        self.assertEqual(huffman.compress(bytes(data)), huffman.compress(bytes(data)))

    def test_output_is_bytes_and_self_contained(self):
        blob = huffman.compress(b"hello")
        self.assertIsInstance(blob, bytes)
        self.assertTrue(blob.startswith(huffman.MAGIC))


if __name__ == "__main__":
    unittest.main(verbosity=2)
