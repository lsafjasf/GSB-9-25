"""Reproduction tests for the five production defects in the legacy v1 codec.

Every test here documents *buggy* behaviour that is actually observed from
``mini_compress.legacy``.  They are the fixed targets: after replacing the
implementation with v2 (``mini_compress.compress2``), the regression suite in
``test_regression.py`` asserts the corrected behaviour.  Keeping these tests
green also proves that the legacy module stays capable of reading history.

Run: python3 -m unittest tests.test_reproduce -v
"""

import os
import subprocess
import sys
import unittest

from mini_compress import legacy


def lcg_bytes(n, seed=0x1234):
    """Deterministic pseudo-random bytes; for seed/length pairs used below the
    output contains no repeated 3-gram, so LZSS can only emit literals."""
    x = seed
    out = bytearray()
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0xFFFFFFFF
        out.append(((x >> 16) & 0xFF) ^ (x & 0xFF))
    return bytes(out)


# Fixed, audited samples: tiny / repeated / high-bit / pseudo-random.
EMPTY = b""
TINY = b"\x00"
REPEATED = b"A" * 64
HIGH_BITS = bytes(range(0x80, 0x100)) * 2
RANDOM = lcg_bytes(32)
assert len({RANDOM[i:i + 3] for i in range(len(RANDOM) - 2)}) == len(RANDOM) - 2


class TestDefect1Expansion(unittest.TestCase):
    """Compressed output can be larger than the raw input."""

    def test_random_data_expands(self):
        frame = legacy.compress(RANDOM)
        # Header 5 + 2 bytes per literal = 69 > 32.
        self.assertGreater(len(frame), len(RANDOM))

    def test_tiny_input_expands(self):
        frame = legacy.compress(TINY)
        self.assertGreater(len(frame), len(TINY))


class TestDefect2TrailingByteLoss(unittest.TestCase):
    """Data ending on a back-recovering match loses its final byte(s)."""

    def test_short_pattern_loses_last_byte(self):
        self.assertEqual(
            legacy.decompress(legacy.compress(b"abcdabcd")), b"abcdabc")

    def test_repeated_bytes_lose_last_byte(self):
        frame = legacy.compress(REPEATED)
        self.assertEqual(legacy.decompress(frame), REPEATED[:-1])

    def test_high_bit_pattern_loses_last_byte(self):
        data = b"\xff\x00\xff\x00\xff\x00"
        self.assertEqual(
            legacy.decompress(legacy.compress(data)), data[:-1])


class TestDefect3Nondeterministic(unittest.TestCase):
    """Identical input yields different bytes in different processes."""

    def test_output_varies_with_hash_seed(self):
        code = (
            "import sys;sys.path.insert(0, %r);"
            "from mini_compress import legacy;"
            "sys.stdout.buffer.write(legacy.compress(b'ab' * 30))"
            % os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        )
        outputs = set()
        base_env = {k: v for k, v in os.environ.items()
                    if k in ("PATH", "SYSTEMROOT", "LD_LIBRARY_PATH", "LANG")}
        for seed in range(6):
            env = dict(base_env)
            env["PYTHONHASHSEED"] = str(seed)
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, env=env, check=True)
            outputs.add(proc.stdout)
        self.assertGreater(
            len(outputs), 1,
            "expected hash-seed-dependent output, got one constant frame")


class TestDefect4TruncationSilentlyAccepted(unittest.TestCase):
    """Truncated input returns partial data and reports success."""

    def test_truncated_frame_returns_partial(self):
        data = b"the quick brown fox jumps over the lazy dog"
        frame = legacy.compress(data)
        cut = frame[:-10]  # remove whole tokens; length header stays intact
        result = legacy.decompress(cut)  # no exception
        self.assertLess(len(result), len(data))
        self.assertEqual(result, data[:len(result)])


class TestDefect5IndistinguishableErrors(unittest.TestCase):
    """Bad header / truncation / corruption all report the same error."""

    def _error(self, blob):
        try:
            legacy.decompress(blob)
        except RuntimeError as exc:
            return type(exc).__name__, str(exc)
        self.fail("legacy.decompress should have raised")

    def test_all_errors_are_identical(self):
        data = b"the quick brown fox jumps over the lazy dog"
        frame = legacy.compress(data)
        bad_header = self._error(b"XYZ" + frame[3:])
        truncated = self._error(frame[:8])
        corrupt = self._error(
            legacy.MAGIC + b"\x00\x05" + b"\x83\xff\x00\x00")
        self.assertEqual(bad_header, truncated)
        self.assertEqual(bad_header, corrupt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
