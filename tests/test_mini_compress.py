"""Regression tests for the fixed MCP1 codec.

Covers all five production issues:
  1. no expansion: incompressible data is stored (RAW mode).
  2. exact round-trip including tricky trailing bytes (assertion required).
  3. deterministic output, within one process and across fresh processes.
  4. truncated frames always raise, never return partial output.
  5. distinct error classes for header / truncation / checksum failures.
"""

import os
import random
import string
import struct
import subprocess
import sys
import unittest
import zlib

from mini_compress import (
    compress,
    decompress,
    MODE_RAW,
    MODE_LZ,
    HEADER_SIZE,
    MCPError,
    HeaderError,
    TruncatedError,
    ChecksumError,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _frame(mode, data, payload=None, orig_len=None, crc=None):
    if payload is None:
        payload = data
    if orig_len is None:
        orig_len = len(data)
    if crc is None:
        crc = zlib.crc32(data)
    return b"MCP1" + bytes([mode]) + struct.pack(">II", orig_len, crc) + payload


class RoundTripTests(unittest.TestCase):
    """Issue 2: lossless round-trip for every input class."""

    CASES = [
        b"",
        b"\x00",
        b"\x01",
        b"\xff",
        b"\x00\x00",
        b"\x00" * 3,
        b"\x00" * 31,
        b"\x00" * 32,
        b"\x00" * 33,
        b"\x00" * 1000,
        b"a" * 63 + b"b",          # run ending right at literal-run boundary
        b"a" * 62 + b"bc",
        b"abc" * 1 + b"z",         # tiny repeated pattern, unique last byte
        b"abc" * 20 + b"\x01",
        b"zqxj" * 8 + b"\x01",     # exact shape that lost its tail in MC0
        (b"abcde" * 13) + b"Z",
        bytes(range(256)),         # every byte value, incompressible
        bytes(range(255, -1, -1)),
        (b"\x80\xff\x00\x7f") * 64,   # high-bit bytes
        b"The quick brown fox " * 10,
        string.printable.encode() * 3,
        os.urandom(1),
        os.urandom(2),
        os.urandom(16),
        os.urandom(64),
        os.urandom(500),
        b"\xff" * 100,
        bytes([i % 7 for i in range(700)]),
        bytes([(i * 37 + 11) & 0xFF for i in range(400)]),
    ]

    def test_curated_cases_roundtrip_exactly(self):
        for data in self.CASES:
            with self.subTest(n=len(data), head=data[:8]):
                frame = compress(data)
                self.assertEqual(decompress(frame), data)  # exact assertion

    def test_random_property_roundtrip(self):
        rng = random.Random(0xC0FFEE)
        for _ in range(120):
            kind = rng.randrange(6)
            n = rng.randrange(0, 800)
            if kind == 0:
                data = bytes(rng.randrange(256) for _ in range(n))
            elif kind == 1:
                data = bytes([rng.randrange(0, 4)]) * n       # low alphabet
            elif kind == 2:
                data = bytes([rng.choice((0x00, 0x80, 0xFF)) for _ in range(n)])
            elif kind == 3:
                block = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 9)))
                data = (block * (n // max(1, len(block)) + 1))[:n]
            elif kind == 4:
                data = bytes([rng.randrange(128, 256) for _ in range(n)])
            else:
                data = bytes([i & 0xFF for i in range(n)])
            with self.subTest(kind=kind, n=n):
                self.assertEqual(decompress(compress(data)), data)

    def test_back_reference_beyond_4k_window(self):
        # The match offset is a full 28-bit field; a duplicate block placed
        # more than 4 KiB later must still reference the first copy.
        block = b"0123456789ABCDEF" * 256          # 4096 bytes
        gap = bytes((i * 131) & 0xFF for i in range(2048))
        data = block + gap + block
        frame = compress(data)
        self.assertEqual(frame[4], MODE_LZ)
        self.assertEqual(decompress(frame), data)

    def test_long_run_uses_overlapping_copy(self):
        data = b"\xff" * 20000
        frame = compress(data)
        self.assertEqual(frame[4], MODE_LZ)
        self.assertLess(len(frame), len(data) // 10)
        self.assertEqual(decompress(frame), data)

    def test_bytearray_and_memoryview_inputs(self):
        data = b"abracadabra" * 5
        ref = compress(data)
        self.assertEqual(compress(bytearray(data)), ref)
        self.assertEqual(compress(memoryview(data)), ref)
        self.assertEqual(decompress(memoryview(ref)), data)


class DeterminismTests(unittest.TestCase):
    """Issue 3: same input -> identical bytes, always."""

    DATA = [
        b"abcde" * 13,
        b"abcabcabc" * 30,
        os.urandom(300),
        bytes(range(256)),
        b"",
        b"\x00" * 500,
    ]

    def test_repeated_calls_identical_in_process(self):
        for data in self.DATA:
            frames = {compress(data) for _ in range(6)}
            self.assertEqual(len(frames), 1, "compression is not deterministic")

    def test_deterministic_across_fresh_processes(self):
        data = b"abcde" * 13
        local = compress(data).hex()
        code = (
            "import sys; sys.path.insert(0, %r);"
            "from mini_compress import compress;"
            "print(compress(%r).hex())" % (_ROOT, data)
        )
        outs = set()
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, check=True,
            )
            outs.add(proc.stdout.strip())
        self.assertEqual(outs, {local})


class StorageModeTests(unittest.TestCase):
    """Issue 1: RAW storage whenever compression does not strictly pay."""

    def test_incompressible_data_is_stored_raw_and_complete(self):
        data = bytes(range(256))
        frame = compress(data)
        self.assertEqual(frame[4], MODE_RAW)
        # RAW frame = fixed header + verbatim original, nothing more.
        self.assertEqual(frame, b"MCP1" + bytes([MODE_RAW])
                         + struct.pack(">II", len(data), zlib.crc32(data)) + data)
        self.assertEqual(len(frame), HEADER_SIZE + len(data))
        self.assertEqual(decompress(frame), data)

    def test_empty_and_tiny_inputs_stored_raw(self):
        for data in (b"", b"\x00", b"ab", b"abc", b"abcd"):
            frame = compress(data)
            self.assertEqual(frame[4], MODE_RAW)
            self.assertEqual(decompress(frame), data)

    def test_lz_frame_must_be_strictly_smaller(self):
        rng = random.Random(1234)
        for _ in range(60):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300)))
            frame = compress(data)
            if frame[4] == MODE_LZ:
                self.assertLess(len(frame), HEADER_SIZE + len(data))
            else:
                self.assertEqual(frame[4], MODE_RAW)

    def test_compressible_data_uses_lz_and_saves_space(self):
        data = b"abcdefgh" * 100
        frame = compress(data)
        self.assertEqual(frame[4], MODE_LZ)
        self.assertLess(len(frame), len(data))
        self.assertEqual(decompress(frame), data)

    def test_tie_goes_to_raw(self):
        # Policy: LZ is chosen only on strictly smaller frames.  Feed the
        # selector a synthetic LZ payload exactly as large as the input and
        # confirm RAW wins; a payload one byte smaller makes LZ win.
        from mini_compress import mini_compress as mc

        data = b"abcdefgh" * 4
        n = len(data)
        real = mc._lz_encode

        mc._lz_encode = lambda d: bytearray(b"\x00") + b"P" * (n - 1)
        try:
            frame = compress(data)
            self.assertEqual(frame[4], MODE_RAW)
            self.assertEqual(len(frame), HEADER_SIZE + n)

            mc._lz_encode = lambda d: bytearray(b"\x00") + b"P" * (n - 2)
            frame = compress(data)
            self.assertEqual(frame[4], MODE_LZ)  # strictly smaller -> LZ
        finally:
            mc._lz_encode = real


class ErrorTaxonomyTests(unittest.TestCase):
    """Issue 5: distinguishable, typed errors."""

    def test_error_hierarchy(self):
        for cls in (HeaderError, TruncatedError, ChecksumError):
            self.assertTrue(issubclass(cls, MCPError))
        self.assertEqual(len({HeaderError, TruncatedError, ChecksumError}), 3)

    def test_illegal_header_cases(self):
        data = b"abcabcabc" * 5
        frame = compress(data)
        cases = {
            "bad magic": b"XXXX" + frame[4:],
            "unknown mode": frame[:4] + b"\x09" + frame[5:],
            # Legacy MC0 frame (magic b"MC") padded beyond header length:
            # must be rejected as an illegal MCP1 header, never decoded.
            "legacy MC0 frame": b"MC\x00\x05\x45hello\x00pad",
        }
        for label, blob in cases.items():
            with self.subTest(label):
                with self.assertRaises(HeaderError):
                    decompress(blob)

    def test_reserved_token_is_header_error(self):
        # 0x50 is in the reserved 0x40..0x7F range; declared length matches
        # a structurally unreachable payload so grammar wins.
        frame = _frame(MODE_LZ, b"abc123", payload=b"\x50\x00")
        with self.assertRaises(HeaderError):
            decompress(frame)

    def test_back_reference_before_start_is_header_error(self):
        payload = b"\x03abc" + bytes([0x80, 0x00, 0x00, 0x09, 0x00]) + b"\x00"
        frame = _frame(MODE_LZ, b"abc" + b"c" * 3, payload=payload, orig_len=6)
        with self.assertRaises(HeaderError):
            decompress(frame)

    def test_truncated_cases(self):
        data = b"abcabcabc" * 5
        frame = compress(data)
        blobs = [
            b"",                                   # empty
            b"MCP",                                # mid-magic
            frame[:12],                            # mid-header
            frame[:HEADER_SIZE],                   # zero payload, LZ mode
            frame[:-1],                            # one byte missing
            frame[: len(frame) // 2],              # cut in half
        ]
        for blob in blobs:
            with self.subTest(n=len(blob)):
                with self.assertRaises(TruncatedError):
                    decompress(blob)

    def test_truncated_raw_declared_length(self):
        frame = _frame(MODE_RAW, b"abcdefgh", payload=b"abc")
        with self.assertRaises(TruncatedError):
            decompress(frame)

    def test_truncated_lz_literal_run(self):
        # literal tag says 5 bytes, only 2 follow and no end tag.
        frame = _frame(MODE_LZ, b"abcde", payload=b"\x05ab")
        with self.assertRaises(TruncatedError):
            decompress(frame)

    def test_truncated_lz_match_token(self):
        frame = _frame(MODE_LZ, b"abcabc", payload=b"\x03abc\x80\x00\x00")
        with self.assertRaises(TruncatedError):
            decompress(frame)

    def test_checksum_failure_on_raw(self):
        frame = bytearray(_frame(MODE_RAW, b"hello world",
                                 payload=b"hello worlD"))
        with self.assertRaises(ChecksumError):
            decompress(bytes(frame))

    def test_checksum_failure_on_lz_literal_byte(self):
        data = b"abcdefghij" * 20
        frame = bytearray(compress(data))
        self.assertEqual(frame[4], MODE_LZ)
        # Flip a byte inside the first literal run (payload starts at 13;
        # byte 14 is literal content, so grammar stays valid).
        frame[14] ^= 0x01
        with self.assertRaises(ChecksumError):
            decompress(bytes(frame))

    def test_trailing_bytes_after_end_tag_rejected(self):
        data = b"abcdefghij" * 20
        self.assertEqual(compress(data)[4], MODE_LZ)
        frame = bytearray(compress(data))
        frame += b"X"
        with self.assertRaises(HeaderError):
            decompress(bytes(frame))

    def test_declared_length_mismatch_rejected(self):
        data = b"abcabcabc" * 2
        frame = bytearray(compress(data))
        struct.pack_into(">I", frame, 5, len(data) + 1)  # header lies
        with self.assertRaises(TruncatedError):
            decompress(bytes(frame))


class CorruptionFuzzTests(unittest.TestCase):
    """Issues 4+5: every truncation/corruption raises a typed error and
    never leaks partial output."""

    def test_every_truncation_raises(self):
        rng = random.Random(99)
        samples = [
            b"abcabcabc" * 20,
            b"zqxj" * 8 + b"\x01",
            bytes(range(256)),
            bytes(rng.randrange(256) for _ in range(300)),
        ]
        for data in samples:
            frame = compress(data)
            for cut in range(0, len(frame)):
                try:
                    out = decompress(frame[:cut])
                except MCPError:
                    continue
                self.fail("truncation at %d returned %r instead of raising"
                          % (cut, out[:8]))

    def test_single_byte_mutations_typed_or_identical(self):
        rng = random.Random(7)
        data = b"abcdefgh" * 20
        frame = bytearray(compress(data))
        for pos in range(4, len(frame)):  # 0..3 magic covered elsewhere
            saved = frame[pos]
            frame[pos] = saved ^ (1 << rng.randrange(8))
            try:
                out = decompress(bytes(frame))
            except MCPError:
                pass  # any typed error is acceptable for corruption
            else:
                self.assertEqual(out, data, "mutation %d silently changed data"
                                 % pos)
            frame[pos] = saved


class SizeComparisonTests(unittest.TestCase):
    """Documented size behaviour across distributions."""

    def test_documented_distributions(self):
        samples = {
            "empty": b"",
            "1 byte": b"\x00",
            "repeat byte x100": b"\x00" * 100,
            "periodic text x100": b"abcdefgh" * 100,
            "high-bit bytes x200": bytes([0x80 | (i & 0x7F) for i in range(200)]),
            "pseudo-random 512": bytes(
                ((i * 1103515245 + 12345) >> 8) & 0xFF for i in range(512)),
            "all 256 byte values": bytes(range(256)),
        }
        for name, data in samples.items():
            frame = compress(data)
            mode = "RAW" if frame[4] == MODE_RAW else "LZ"
            # Invariant used in FORMAT.md: frame <= header + input.
            self.assertLessEqual(len(frame), HEADER_SIZE + len(data))
            if mode == "LZ":
                self.assertLess(len(frame), HEADER_SIZE + len(data))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class EncoderSearchTests(unittest.TestCase):
    """The capped-scan/hash-chain search must emit exactly the tokens a
    full backward scan would (nearest match, strict '>' tie-break)."""

    @staticmethod
    def _reference_scan(data):
        from mini_compress.mini_compress import (
            MAX_LITERAL, MAX_MATCH_ADD, MAX_OFFSET,
        )
        payload = bytearray()
        n = len(data)
        i = 0
        while i < n:
            best_len = 0
            best_off = 0
            if i + 3 <= n:
                limit = min(n - i, MAX_MATCH_ADD + 3)
                for p in range(i - 1, -1, -1):
                    off = i - p
                    if off > MAX_OFFSET:
                        break
                    ml = 0
                    while ml < limit and data[p + ml] == data[i + ml]:
                        ml += 1
                    if ml > best_len:
                        best_len = ml
                        best_off = off
                        if ml == limit:
                            break
            if best_len >= 3:
                payload.append(0x80 | ((best_off >> 24) & 0x0F))
                payload.append((best_off >> 16) & 0xFF)
                payload.append((best_off >> 8) & 0xFF)
                payload.append(best_off & 0xFF)
                payload.append(best_len - 3)
                i += best_len
            else:
                j = min(i + MAX_LITERAL, n)
                payload.append(j - i)
                payload += data[i:j]
                i = j
        payload.append(0x00)
        return payload

    def test_encoder_matches_full_scan_byte_for_byte(self):
        from mini_compress.mini_compress import _lz_encode
        rng = random.Random(2024)
        cases = [
            b"", b"\x00", b"ab", b"abc", b"\x00" * 300,
            b"abc" * 100, b"abcdefgh" * 50, bytes(range(256)),
            bytes(range(255, -1, -1)), b"a" * 63 + b"b",
            bytes([i & 3 for i in range(500)]),
            # Force the hash-chain path (scan exceeds _SCAN_CAP) ...
            os.urandom(2000),
            os.urandom(1) * 300 + os.urandom(1500),   # mixed density
            os.urandom(1500) + b"abc" * 400,          # chain then matches
        ]
        for _ in range(80):
            kind = rng.randrange(5)
            n = rng.randrange(0, 1500)
            if kind == 0:
                data = bytes(rng.randrange(256) for _ in range(n))
            elif kind == 1:
                data = bytes([rng.randrange(4)]) * n
            elif kind == 2:
                block = bytes(rng.randrange(256)
                              for _ in range(rng.randrange(1, 9)))
                data = (block * (n // max(1, len(block)) + 1))[:n]
            elif kind == 3:
                data = bytes([rng.choice((0x00, 0x80, 0xFF))
                              for _ in range(n)])
            else:
                data = bytes([i & 0xFF for i in range(n)])
            cases.append(data)
        for data in cases:
            with self.subTest(n=len(data), head=data[:8]):
                self.assertEqual(bytes(_lz_encode(data)),
                                 bytes(self._reference_scan(data)))


class SizeLimitTests(unittest.TestCase):
    """Compress and decompress must enforce the same decoded-size ceiling."""

    def test_compress_uses_decoder_limit(self):
        import mini_compress.mini_compress as mc
        # The constant the compressor checks is the one the decoder enforces.
        data = b"\x00" * (mc.MAX_DECODED_SIZE + 1)
        with self.assertRaises(HeaderError):
            mc.compress(data)

    def test_oversized_input_rejected_before_encoding(self):
        import mini_compress.mini_compress as mc
        real_limit = mc.MAX_DECODED_SIZE
        mc.MAX_DECODED_SIZE = 8
        try:
            self.assertEqual(decompress(compress(b"\x00" * 8)), b"\x00" * 8)
            with self.assertRaises(HeaderError):
                compress(b"\x00" * 9)
            # And the decoder independently refuses a frame that declares
            # more than the limit.
            frame = _frame(MODE_RAW, b"x" * 9, payload=b"x" * 9)
            with self.assertRaises(HeaderError):
                decompress(frame)
        finally:
            mc.MAX_DECODED_SIZE = real_limit
