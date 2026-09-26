"""Differential tests: refactored parser must equal the legacy monolith
on every input, byte for byte (structure AND error dicts).

Comparison is type-strict: int 1 and float 1.0 are considered different
even though ``1 == 1.0`` in Python.
"""

import random
import unittest

import legacy_parser
from filter_parser import parse as staged_parse

from tests.corpus import CASES, gen_mutation, gen_soup, gen_valid

FUZZ_CASES = 3000
FUZZ_SEED = 20260926


def strict_equal(a, b, path="$"):
    """Deep equality that also distinguishes int from float."""
    if type(a) is not type(b):
        return "type mismatch at %s: %r (%s) vs %r (%s)" % (
            path, a, type(a).__name__, b, type(b).__name__)
    if isinstance(a, dict):
        if set(a) != set(b):
            return "key mismatch at %s: %r vs %r" % (path, sorted(a), sorted(b))
        for key in a:
            problem = strict_equal(a[key], b[key], "%s.%s" % (path, key))
            if problem:
                return problem
        return None
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return "length mismatch at %s" % path
        for idx, (x, y) in enumerate(zip(a, b)):
            problem = strict_equal(x, y, "%s[%d]" % (path, idx))
            if problem:
                return problem
        return None
    if a != b:
        return "value mismatch at %s: %r vs %r" % (path, a, b)
    return None


class DifferentialTests(unittest.TestCase):
    def assert_same(self, text, label):
        legacy = legacy_parser.parse(text)
        staged = staged_parse(text)
        problem = strict_equal(legacy, staged)
        self.assertIsNone(
            problem,
            "divergence on %s input %r:\n%s\nlegacy=%r\nstaged=%r"
            % (label, text, problem, legacy, staged),
        )

    def test_handwritten_corpus(self):
        for name, text in CASES:
            with self.subTest(case=name):
                self.assert_same(text, name)

    def test_fuzz_valid(self):
        rng = random.Random(FUZZ_SEED)
        for k in range(FUZZ_CASES):
            self.assert_same(gen_valid(rng), "fuzz-valid-%d" % k)

    def test_fuzz_soup(self):
        rng = random.Random(FUZZ_SEED + 1)
        for k in range(FUZZ_CASES):
            self.assert_same(gen_soup(rng), "fuzz-soup-%d" % k)

    def test_fuzz_mutation(self):
        rng = random.Random(FUZZ_SEED + 2)
        for k in range(FUZZ_CASES):
            self.assert_same(gen_mutation(rng), "fuzz-mutation-%d" % k)


if __name__ == "__main__":
    unittest.main()
