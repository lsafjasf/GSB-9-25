"""Total-order property assertions.

Verifies that the comparison is a strict total order (modulo the documented
equivalence class of build metadata) and -- the key required assertion --
that sorting by the comparator agrees with every pairwise comparison.
"""

import functools
import itertools
import random
import unittest

from tests import *  # noqa: F401,F403  (sys.path setup)
from semverlib import Version, compare

_SEED = 20260926
_POOL_SIZE = 300
_TRANSITIVE_SAMPLES = 60000


def _random_version(rng):
    major = rng.randint(0, 5) if rng.random() < 0.9 else rng.randint(0, 10 ** 6)
    minor = rng.randint(0, 5)
    patch = rng.randint(0, 5)
    pre = ()
    if rng.random() < 0.5:
        choices = ["0", "1", "2", "11", "alpha", "beta", "rc", "x-y"]
        pre = tuple(rng.choice(choices) for _ in range(rng.randint(1, 3)))
    build = ()
    if rng.random() < 0.3:
        build = tuple(rng.choice(["b1", "b2", "001", "sha"]) for _ in range(rng.randint(1, 2)))
    return Version(major, minor, patch, pre, build)


class TotalOrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = random.Random(_SEED)
        fixed = [Version.parse(s) for s in (
            "0.0.0", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta",
            "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1",
            "1.0.0", "1.0.0+build", "2.0.0",
        )]
        cls.pool = fixed + [_random_version(rng) for _ in range(_POOL_SIZE)]

    def test_totality_and_operator_consistency(self):
        for a, b in itertools.product(self.pool, repeat=2):
            c = compare(a, b)
            self.assertIn(c, (-1, 0, 1))
            self.assertEqual(c == 0, a == b)
            self.assertEqual(c < 0, a < b)
            self.assertEqual(c > 0, a > b)
            self.assertEqual(c <= 0, a <= b)
            self.assertEqual(c >= 0, a >= b)
            self.assertEqual(c != 0, a != b)

    def test_antisymmetry(self):
        for a, b in itertools.product(self.pool, repeat=2):
            self.assertEqual(compare(a, b), -compare(b, a))

    def test_transitivity(self):
        rng = random.Random(_SEED + 1)
        for _ in range(_TRANSITIVE_SAMPLES):
            a, b, c = (rng.choice(self.pool) for _ in range(3))
            if a <= b and b <= c:
                self.assertLessEqual(a, c, "%s <= %s <= %s" % (a, b, c))
            if a < b and b < c:
                self.assertLess(a, c, "%s < %s < %s" % (a, b, c))

    def test_sorting_agrees_with_pairwise_comparison(self):
        """The headline assertion: the order produced by sorting with the
        comparator must be consistent with every pairwise comparison."""
        rng = random.Random(_SEED + 2)
        shuffled = list(self.pool)
        rng.shuffle(shuffled)
        ordered = sorted(shuffled, key=functools.cmp_to_key(compare))
        self.assertEqual(len(ordered), len(self.pool))
        self.assertEqual(sorted(map(id, ordered)), sorted(map(id, self.pool)))
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                self.assertLessEqual(
                    compare(ordered[i], ordered[j]), 0,
                    "sorted[%d]=%s !<= sorted[%d]=%s"
                    % (i, ordered[i], j, ordered[j]),
                )
        # And conversely: the sign of any pairwise comparison predicts the
        # relative position in the sorted output.
        position = {id(v): i for i, v in enumerate(ordered)}
        for a, b in itertools.product(self.pool, repeat=2):
            c = compare(a, b)
            if c < 0:
                self.assertLess(position[id(a)], position[id(b)])
            elif c > 0:
                self.assertGreater(position[id(a)], position[id(b)])

    def test_total_key_gives_deterministic_strict_order(self):
        # Precedence-equal versions (different build metadata) still get a
        # single deterministic order under total_key, from any shuffling.
        rng = random.Random(_SEED + 3)
        variants = [Version.parse(s) for s in (
            "1.0.0", "1.0.0+a", "1.0.0+b.2", "1.0.0+001", "1.0.0+a.b",
            "0.9.9+z", "1.0.0-rc.1+x", "1.0.0-rc.1",
        )]
        orders = set()
        for _ in range(50):
            shuffled = list(variants)
            rng.shuffle(shuffled)
            orders.add(tuple(sorted(shuffled, key=Version.total_key)))
        self.assertEqual(len(orders), 1)
        keys = [v.total_key() for v in variants]
        self.assertEqual(len(set(keys)), len(keys))  # no collisions at all


if __name__ == "__main__":
    unittest.main()
