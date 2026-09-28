"""Regression tests for the fixed v2 codec (``mini_compress.compress2``).

Coverage required by the fix request:

* lossless round trip for tiny inputs, repeated bytes, high-bit bytes,
  pseudo-random and random inputs and a long high-entropy stream;
* determinism: compressing the same input twice (and across processes with
  different PYTHONHASHSEED) yields identical bytes;
* explicit STORED mode whenever DEFLATE does not pay off, plus payload never
  larger than the raw input and fixed 17-byte overhead;
* distinct errors for truncation / checksum failure / illegal header /
  corrupt payload, with no partial output on any failure.

Run: python3 -m unittest tests.test_regression -v
"""

import os
import random
import subprocess
import sys
import unittest
import zlib

from mini_compress import compress2 as c2
from mini_compress import legacy


def lcg_bytes(n, seed=0x1234):
    x = seed
    out = bytearray()
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0xFFFFFFFF
        out.append(((x >> 16) & 0xFF) ^ (x & 0xFF))
    return bytes(out)


TINY_SAMPLES = [b"", b"\x00", b"\xff", b"\x00\x01", b"ab", bytes(range(8))]
REPEATED_SAMPLES = [b"A" * 64, b"\x00" * 100, b"\xff" * 257,
                    (b"ab" * 50), (bytes([0xAB, 0xCD]) * 100)]
HIGH_BIT_SAMPLES = [bytes(range(0x80, 0x100)),
                    bytes(range(0x80, 0x100)) * 4,
                    b"\xff" * 16 + b"\x00" * 16,
                    bytes((b * 7 + 13) & 0xFF for b in range(2000))]
RANDOM_SAMPLES = [lcg_bytes(0), lcg_bytes(1), lcg_bytes(16), lcg_bytes(32),
                  lcg_bytes(200), lcg_bytes(4096, seed=7)]
ALL_SAMPLES = TINY_SAMPLES + REPEATED_SAMPLES + HIGH_BIT_SAMPLES + RANDOM_SAMPLES


class TestRoundTrip(unittest.TestCase):
    """Defect #2: every input must survive compress -> decompress exactly."""

    def _assert_roundtrip(self, data):
        frame = c2.compress(data)
        self.assertEqual(c2.decompress(frame), data)

    def test_tiny_inputs(self):
        for data in TINY_SAMPLES:
            self._assert_roundtrip(data)

    def test_repeated_bytes(self):
        for data in REPEATED_SAMPLES:
            self._assert_roundtrip(data)

    def test_high_bit_bytes(self):
        for data in HIGH_BIT_SAMPLES:
            self._assert_roundtrip(data)

    def test_pseudo_random_and_random(self):
        for data in RANDOM_SAMPLES:
            self._assert_roundtrip(data)
        rng = random.Random(20260926)
        for _ in range(50):
            n = rng.randrange(0, 600)
            self._assert_roundtrip(bytes(rng.randrange(256) for _ in range(n)))

    def test_long_high_entropy(self):
        self._assert_roundtrip(os.urandom(10000))

    def test_bytearray_and_memoryview_input(self):
        data = b"typed inputs \x00\xff" * 10
        self.assertEqual(c2.decompress(c2.compress(bytearray(data))), data)
        self.assertEqual(c2.decompress(c2.compress(memoryview(data))), data)


class TestDeterminism(unittest.TestCase):
    """Defect #3: same input -> identical compressed bytes."""

    DATA = [b"", b"x", b"ab" * 30, bytes(range(256)) * 3,
            lcg_bytes(123, seed=99), os.urandom(777)]

    def test_repeated_compression_identical(self):
        for data in self.DATA:
            first = c2.compress(data)
            for _ in range(5):
                self.assertEqual(c2.compress(data), first)

    def test_independent_of_python_hash_seed(self):
        code = (
            "import sys;sys.path.insert(0, %r);"
            "from mini_compress import compress2 as c;"
            "sys.stdout.buffer.write(c.compress(b'ab' * 30))"
            % os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        )
        base_env = {k: v for k, v in os.environ.items()
                    if k in ("PATH", "SYSTEMROOT", "LD_LIBRARY_PATH", "LANG")}
        outputs = set()
        for seed in range(6):
            env = dict(base_env)
            env["PYTHONHASHSEED"] = str(seed)
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, env=env, check=True)
            outputs.add(proc.stdout)
        self.assertEqual(len(outputs), 1)


class TestStoredModeAndSize(unittest.TestCase):
    """Defect #1: never silently expand; explicit STORED for bad ratios."""

    def test_random_data_uses_stored_mode(self):
        data = lcg_bytes(32)
        frame = c2.compress(data)
        self.assertEqual(frame[4] & 0x01, c2.MODE_STORED)
        self.assertEqual(frame[c2.HEADER_LEN:], data)

    def test_empty_and_tiny_use_stored_mode(self):
        for data in (b"", b"\x00", b"ab"):
            frame = c2.compress(data)
            self.assertEqual(frame[4] & 0x01, c2.MODE_STORED)

    def test_compressible_data_uses_deflate_mode(self):
        frame = c2.compress(b"A" * 1000)
        self.assertEqual(frame[4] & 0x01, c2.MODE_DEFLATE)
        self.assertLess(len(frame), 1000)

    def test_payload_never_exceeds_raw_size(self):
        rng = random.Random(42)
        for _ in range(100):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 500)))
            frame = c2.compress(data)
            payload_len = int.from_bytes(frame[9:13], "big")
            self.assertLessEqual(payload_len, len(data))

    def test_constant_header_overhead(self):
        for data in ALL_SAMPLES:
            frame = c2.compress(data)
            payload_len = int.from_bytes(frame[9:13], "big")
            self.assertEqual(len(frame), c2.HEADER_LEN + payload_len)


def _forge(mode, payload, original_len, crc=None):
    if crc is None:
        crc = zlib.crc32(payload) & 0xFFFFFFFF
    return (b"MC2" + bytes([c2.VERSION, mode])
            + original_len.to_bytes(4, "big")
            + len(payload).to_bytes(4, "big")
            + crc.to_bytes(4, "big") + payload)


def _raw_deflate(data):
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    return co.compress(data) + co.flush()


class TestTruncatedError(unittest.TestCase):
    """Defect #4: truncated frames raise, never return partial data."""

    def setUp(self):
        self.data = b"the quick brown fox " * 20
        self.frame = c2.compress(self.data)

    def test_every_prefix_is_rejected(self):
        for cut in range(0, len(self.frame)):
            with self.assertRaises(c2.CompressionError):
                c2.decompress(self.frame[:cut])

    def test_short_prefix_is_truncated_not_header(self):
        for cut in (0, 1, 16):
            with self.assertRaises(c2.TruncatedError):
                c2.decompress(self.frame[:cut])

    def test_missing_payload_bytes_are_truncated(self):
        bad = self.frame[:c2.HEADER_LEN + 1]
        with self.assertRaises(c2.TruncatedError):
            c2.decompress(bad)

    def test_stored_truncation_raises(self):
        frame = c2.compress(lcg_bytes(40))
        with self.assertRaises(c2.TruncatedError):
            c2.decompress(frame[:-3])

    def test_no_partial_output_observable(self):
        # The API returns either the full bytes or raises; nothing partial.
        for cut in (17, 20, len(self.frame) - 1):
            try:
                out = c2.decompress(self.frame[:cut])
            except c2.CompressionError:
                continue
            self.assertEqual(out, self.data)


class TestChecksumError(unittest.TestCase):
    """Defect #5: payload bit flips are CRC failures."""

    def test_stored_payload_bitflip(self):
        frame = bytearray(c2.compress(lcg_bytes(40)))
        frame[-1] ^= 0xFF
        with self.assertRaises(c2.ChecksumError):
            c2.decompress(bytes(frame))

    def test_deflate_payload_bitflip(self):
        frame = bytearray(c2.compress(b"A" * 500))
        frame[20] ^= 0x01
        with self.assertRaises(c2.ChecksumError):
            c2.decompress(bytes(frame))

    def test_header_crc_field_altered(self):
        frame = bytearray(c2.compress(b"A" * 500))
        frame[13] ^= 0xFF
        with self.assertRaises(c2.ChecksumError):
            c2.decompress(bytes(frame))


class TestCorruptPayloadError(unittest.TestCase):
    """CRC-valid but undecodable deflate / wrong declared length."""

    def test_crc_valid_but_invalid_deflate_stream(self):
        frame = _forge(c2.MODE_DEFLATE, b"\x07\x00garbage", 7)
        with self.assertRaises(c2.CorruptPayloadError):
            c2.decompress(frame)

    def test_prematurely_ending_deflate_stream_is_rejected(self):
        # Forged frame whose header fields are all self-consistent
        # (original_len / payload_len / crc32 match the payload), but the
        # DEFLATE payload is a single NON-final stored block with no final
        # block after it: it decodes to exactly the declared length yet the
        # stream never terminates.  Must not be accepted as success.
        data = b"premature end, full length"
        payload = (b"\x00"  # BFINAL=0, BTYPE=00 (stored, non-final)
                   + len(data).to_bytes(2, "little")
                   + (0xFFFF - len(data)).to_bytes(2, "little") + data)
        dec = zlib.decompressobj(-15)
        self.assertEqual(dec.decompress(payload), data)
        self.assertFalse(dec.eof)  # stream really does end early
        frame = _forge(c2.MODE_DEFLATE, payload, len(data))
        with self.assertRaises(c2.CorruptPayloadError):
            c2.decompress(frame)

    def test_crc_valid_but_decoded_length_mismatch(self):
        frame = _forge(c2.MODE_DEFLATE, _raw_deflate(b"hello"), 99)
        with self.assertRaises(c2.CorruptPayloadError):
            c2.decompress(frame)

    def test_corrupt_payload_is_a_checksum_failure_too(self):
        frame = _forge(c2.MODE_DEFLATE, b"\x07\x00garbage", 7)
        with self.assertRaises(c2.ChecksumError):
            c2.decompress(frame)


class TestHeaderError(unittest.TestCase):
    """Defect #5: illegal headers are reported distinctly."""

    def setUp(self):
        self.frame = bytearray(c2.compress(b"A" * 50))

    def test_bad_magic(self):
        bad = b"XXX" + bytes(self.frame[3:])
        with self.assertRaises(c2.HeaderError):
            c2.decompress(bad)

    def test_bad_version(self):
        bad = bytes(self.frame[:3]) + bytes([9]) + bytes(self.frame[4:])
        with self.assertRaises(c2.HeaderError):
            c2.decompress(bad)

    def test_reserved_flag_bits(self):
        for flag in (0x02, 0x80, 0xFE):
            bad = bytes(self.frame[:4]) + bytes([flag]) + bytes(self.frame[5:])
            with self.assertRaises(c2.HeaderError):
                c2.decompress(bad)

    def test_stored_length_contradiction(self):
        frame = _forge(c2.MODE_STORED, b"abc", 4)
        with self.assertRaises(c2.HeaderError):
            c2.decompress(frame)

    def test_trailing_bytes_after_payload(self):
        with self.assertRaises(c2.HeaderError):
            c2.decompress(bytes(self.frame) + b"\x00\x01")

    def test_error_types_are_distinct(self):
        frame = bytes(self.frame)
        with self.assertRaises(c2.TruncatedError):
            c2.decompress(frame[:10])
        bad = bytearray(frame)
        bad[-1] ^= 0xFF
        with self.assertRaises(c2.ChecksumError):
            c2.decompress(bytes(bad))
        self.assertNotEqual(c2.TruncatedError, c2.ChecksumError)
        self.assertNotEqual(c2.HeaderError, c2.ChecksumError)
        self.assertTrue(issubclass(c2.CorruptPayloadError, c2.ChecksumError))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMigration(unittest.TestCase):
    """Historical v1 frames migrate to v2; damaged ones are refused."""

    def test_lossless_v1_frame_migrates(self):
        from migrate import migrate_frame
        frame = legacy.compress(b"legacy block with no end-match here")
        new_frame, status, _ = migrate_frame("v1", frame)
        self.assertEqual(status, "migrated")
        self.assertEqual(c2.decompress(new_frame),
                         b"legacy block with no end-match here")

    def test_v2_frame_is_kept_identically(self):
        from migrate import migrate_frame
        frame = c2.compress(b"already new" * 5)
        new_frame, status, _ = migrate_frame("v2", frame)
        self.assertEqual(status, "kept")
        self.assertEqual(new_frame, frame)

    def test_defect2_v1_frame_is_refused(self):
        from migrate import migrate_frame
        frame = legacy.compress(b"abcdabcd")
        new_frame, status, _ = migrate_frame("v1", frame)
        self.assertIsNone(new_frame)
        self.assertTrue(status.startswith("skipped:truncated"))

    def test_iter_frames_roundtrip_stream(self):
        from migrate import iter_frames
        stream = (legacy.compress(b"first legacy block")
                  + legacy.compress(b"abcdabcd")
                  + c2.compress(b"second new block" * 3)
                  )
        kinds = [kind for kind, _, _ in iter_frames(stream)]
        self.assertEqual(kinds, ["v1", "v1", "v2"])

    def test_trailing_defect_frame_is_indistinguishable_from_truncation(self):
        # v1 carries no token length: an early-ending final frame cannot be
        # told apart from a cut-off stream, so the tool refuses to guess.
        from migrate import iter_frames
        stream = c2.compress(b"fine") + legacy.compress(b"abcdabcd")
        with self.assertRaises(ValueError):
            list(iter_frames(stream))
