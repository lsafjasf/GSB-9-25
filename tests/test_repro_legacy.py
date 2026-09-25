"""Stable reproductions of the five production defects.

Each test targets one defect of the legacy implementation in
``mini_compress.legacy_buggy`` (format "MC0").  The tests are written against
the *observed broken behaviour* and fail if any of those defects silently
disappears, which keeps the reproduction trustworthy.  The fixed format's
guarantees live in ``test_mini_compress.py``.

Defect map:
    test_expansion_on_incompressible_data ..... (1) compressed > original
    test_trailing_byte_dropped ................ (2) last byte lost
    test_non_deterministic_between_calls ...... (3) output varies per call
    test_truncated_input_returns_partial ...... (4) partial = success
    test_error_causes_indistinguishable ....... (5) one exception/message
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LEGACY_PATH = os.path.join(_HERE, "..", "mini_compress", "legacy_buggy.py")


def _load_legacy():
    spec = importlib.util.spec_from_file_location("legacy_buggy", _LEGACY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LegacyDefectTests(unittest.TestCase):
    def setUp(self):
        self.legacy = _load_legacy()
        self.legacy.reset_state()

    # --- (1) size: incompressible data is expanded because there is no
        # stored/raw fallback mode.
    def test_expansion_on_incompressible_data(self):
        data = bytes(range(256))  # all byte values, no repeated 3-grams
        frame = self.legacy.compress(data)
        self.assertGreater(
            len(frame), len(data),
            "legacy codec should expand incompressible data",
        )

    # --- (2) the final byte, when not covered by a back-reference, is
        # dropped by the encoder's len-1 loop bound.
    def test_trailing_byte_dropped(self):
        data = (b"zqxj" * 8) + b"\x01"  # high-periodic body + unique tail
        out = self.legacy.decompress(self.legacy.compress(data))
        self.assertEqual(out, data[:-1])
        self.assertEqual(len(out), len(data) - 1)

    # --- (3) module-level rotation state leaks between calls, so two
        # compressions of the same bytes differ within one process.
    def test_non_deterministic_between_calls(self):
        data = b"abcde" * 13  # every match has >=2 equal-length candidates
        first = self.legacy.compress(data)
        second = self.legacy.compress(data)
        self.assertNotEqual(first, second)
        # Both frames still decode (to the same lossy result); the defect is
        # specifically the unstable encoding, not a crash.
        self.assertEqual(
            self.legacy.decompress(first), self.legacy.decompress(second)
        )

    # --- (4) truncated frames silently return whatever prefix was decoded.
    def test_truncated_input_returns_partial(self):
        data = b"hello"  # encoded as a single literal run + end tag
        frame = self.legacy.compress(data)
        # Cut inside the literal run: tag declares 5 bytes, only 3 remain,
        # and there is no end tag.  The legacy decoder returns the prefix
        # without raising.
        cut = frame[:-2]
        out = self.legacy.decompress(cut)
        self.assertEqual(out, b"hel")  # partial output reported as success
        self.assertTrue(data.startswith(out) and out != data)
        # Cutting only the final literal byte is likewise "successful".
        self.assertEqual(self.legacy.decompress(frame[:-1]), data[:-1])

    # --- (5) every failure is the same Exception("bad data") -- causes are
        # not distinguishable by type or message.
    def test_error_causes_indistinguishable(self):
        cases = {
            "bad magic": b"XX\x00\x00",
            "truncated literal": b"MC\x00\x05\x43abc",  # declares 3, has 3? no
            "reserved token": b"MC\x00\x00\x01\x00",
        }
        cases["truncated literal"] = b"MC\x00\x05\x43ab"  # declares 3, has 2
        seen = set()
        for blob in cases.values():
            try:
                self.legacy.decompress(blob)
            except Exception as exc:  # noqa: BLE001 - the point: base class
                seen.add((type(exc), str(exc)))
        self.assertEqual(len(seen), 1, seen)
        type_, message = next(iter(seen))
        self.assertIs(type_, Exception)
        self.assertEqual(message, "bad data")


if __name__ == "__main__":
    unittest.main(verbosity=2)
