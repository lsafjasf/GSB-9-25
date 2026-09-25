"""Corruption test suite: every damaged archive must raise a distinguishable
HuffmanError subclass and must never yield partial output.

Run: python3 test_corruption.py
"""

import struct
import unittest

import huffman

DATA = b"the quick brown fox jumps over the lazy dog. " * 40 + bytes(range(256))
BLOB = huffman.compress(DATA)


def sections(blob: bytes):
    """Return (table_end, padding_off, digest_off, payload_off)."""
    count = struct.unpack(">H", blob[12:14])[0]
    table_end = 14 + 2 * count
    return table_end, table_end, table_end + 1, table_end + 1 + 16


TABLE_END, PAD_OFF, DIG_OFF, PAY_OFF = sections(BLOB)


def mutated(offset: int, xor: int, blob: bytes = BLOB) -> bytes:
    b = bytearray(blob)
    b[offset] ^= xor
    return bytes(b)


class CorruptionTests(unittest.TestCase):
    def expect(self, exc, blob, label):
        with self.assertRaises(exc, msg=f"{label} did not raise {exc.__name__}"):
            huffman.decompress(blob)

    # --- truncation -------------------------------------------------------
    def test_truncated_magic(self):
        self.expect(huffman.TruncatedError, BLOB[:2], "truncated magic")

    def test_truncated_header(self):
        self.expect(huffman.TruncatedError, BLOB[:10], "truncated header")

    def test_truncated_table(self):
        self.expect(huffman.TruncatedError, BLOB[:TABLE_END - 1], "truncated table")

    def test_truncated_digest(self):
        self.expect(huffman.TruncatedError, BLOB[:PAY_OFF - 3], "truncated digest")

    def test_truncated_payload_half(self):
        cut = PAY_OFF + (len(BLOB) - PAY_OFF) // 2
        self.expect(huffman.TruncatedError, BLOB[:cut], "payload cut in half")

    def test_truncated_payload_last_byte(self):
        self.expect(huffman.TruncatedError, BLOB[:-1], "payload missing last byte")

    # --- header / format --------------------------------------------------
    def test_bad_magic(self):
        self.expect(huffman.FormatError, mutated(0, 0xFF), "bad magic")

    def test_padding_out_of_range(self):
        b = bytearray(BLOB)
        b[PAD_OFF] = 9
        self.expect(huffman.FormatError, bytes(b), "padding > 7")

    # --- code table -------------------------------------------------------
    def test_table_zero_length(self):
        self.expect(huffman.InvalidCodeTableError, mutated(15, BLOB[15]),
                    "code length zero")

    def test_table_duplicate_symbol(self):
        b = bytearray(BLOB)
        b[16] = b[14]  # second entry's symbol = first entry's symbol
        self.expect(huffman.InvalidCodeTableError, bytes(b), "duplicate symbol")

    def test_table_kraft_violation(self):
        # Lengthening one code breaks the Kraft equality.
        self.expect(huffman.InvalidCodeTableError, mutated(15, 0x01),
                    "broken Kraft sum")

    def test_table_swap_mismatch(self):
        # Same alphabet size, different frequencies -> different (but valid)
        # code table; BLOB's bitstream must not decode under it.
        other = huffman.compress(bytes(range(256)) * 40 + b"zzzzzzzz" * 500)
        t_end, _, _, p_off = sections(other)
        merged = BLOB[:14] + other[14:t_end] + BLOB[TABLE_END:]
        with self.assertRaises(huffman.HuffmanError):
            huffman.decompress(merged)

    # --- trailing data ----------------------------------------------------
    def test_extra_bytes_appended(self):
        self.expect(huffman.TrailingDataError, BLOB + b"\x00\x00\x00",
                    "extra trailing bytes")

    def test_empty_stream_with_garbage(self):
        self.expect(huffman.TrailingDataError, huffman.compress(b"") + b"x",
                    "garbage after empty stream")

    def test_padding_too_small(self):
        padding = BLOB[PAD_OFF]
        if padding == 0:
            self.skipTest("archive happens to have zero padding")
        b = bytearray(BLOB)
        b[PAD_OFF] = padding - 1
        self.expect(huffman.TrailingDataError, bytes(b), "padding undercounted")

    def test_padding_too_large(self):
        padding = BLOB[PAD_OFF]
        b = bytearray(BLOB)
        b[PAD_OFF] = padding + 1
        self.expect(huffman.TruncatedError, bytes(b), "padding overcounted")

    def test_nonzero_padding_bits(self):
        padding = BLOB[PAD_OFF]
        if padding == 0:
            self.skipTest("archive happens to have zero padding")
        # Set the lowest padding bit of the final payload byte.
        self.expect(huffman.TrailingDataError, mutated(len(BLOB) - 1, 0x01),
                    "non-zero padding bit")

    # --- table/data mismatch & integrity ----------------------------------
    def test_corrupted_digest(self):
        self.expect(huffman.IntegrityError, mutated(DIG_OFF, 0x01),
                    "digest byte flipped")

    def test_flipped_payload_bit(self):
        # Structurally decodable or not, it must raise, never return data.
        with self.assertRaises(huffman.HuffmanError):
            huffman.decompress(mutated(PAY_OFF + 5, 0x10))

    def test_empty_table_nonzero_length(self):
        # Hand-crafted: count = 0 but original length = 5.
        blob = (huffman.MAGIC + struct.pack(">QH", 5, 0) + b"\x00"
                + bytes(16))
        self.expect(huffman.BitstreamMismatchError, blob, "empty table, len 5")

    def test_no_partial_output(self):
        # decompress() is all-or-nothing: any corruption raises, so there is
        # no way to observe partial data. Verify on a batch of corruptions.
        corruptions = [
            BLOB[:len(BLOB) // 2],
            BLOB + b"junk",
            mutated(DIG_OFF, 0x80),
            mutated(15, 0x01),
        ]
        for blob in corruptions:
            with self.assertRaises(huffman.HuffmanError):
                huffman.decompress(blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
