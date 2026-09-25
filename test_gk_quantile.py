"""Self-tests for gk_quantile (stdlib unittest).

Verifies the reported error bound against exact full-data ranks for
uniform, exponential, bimodal and duplicate-heavy distributions, plus
merge consistency and edge cases (empty, single element, all identical,
tiny memory budget).

Run:  python3 -m unittest test_gk_quantile -v
"""

import bisect
import math
import random
import unittest

from gk_quantile import GKSummary

EPS = 0.01
N = 30_000
QUANTILES = (0.5, 0.9, 0.95, 0.99, 0.999)


def make_data(dist, n, seed=42):
    rng = random.Random(seed)
    if dist == "uniform":
        return [rng.random() for _ in range(n)]
    if dist == "exponential":
        return [rng.expovariate(1.0) for _ in range(n)]
    if dist == "bimodal":
        return [rng.gauss(0.0, 1.0) if rng.random() < 0.5
                else rng.gauss(10.0, 2.0) for _ in range(n)]
    if dist == "duplicates":
        return [42.0 if rng.random() < 0.8 else rng.random()
                for _ in range(n)]
    raise ValueError(dist)


def rank_interval(sorted_data, x):
    """1-based inclusive rank range of value x in sorted_data."""
    return (bisect.bisect_left(sorted_data, x) + 1,
            bisect.bisect_right(sorted_data, x))


def interval_distance(a, b):
    """Gap between two closed intervals; 0 if they overlap."""
    return max(0, a[0] - b[1], b[0] - a[1])


def check_quantile(summary, sorted_data, q):
    """Assert the estimate's true rank is within the reported bound."""
    est = summary.quantile(q)
    bound = summary.rank_error_bound()
    r = max(1, math.ceil(q * len(sorted_data)))
    lo, hi = rank_interval(sorted_data, est)
    assert lo <= r + bound and hi >= r - bound, (
        "q=%g est=%r rank=[%d,%d] target=%d bound=%d"
        % (q, est, lo, hi, r, bound))
    actual = interval_distance((lo, hi), (r, r))
    return est, bound, actual


class TestEdgeCases(unittest.TestCase):
    def test_empty_stream(self):
        s = GKSummary(EPS)
        self.assertEqual(s.n, 0)
        self.assertIsNone(s.quantile(0.5))
        self.assertEqual(s.rank_error_bound(), 0)
        self.assertEqual(s.quantile_interval(0.5), (None, None, None))

    def test_single_element(self):
        s = GKSummary(EPS)
        s.add(7.5)
        for q in (0.0, 0.5, 0.99, 1.0):
            self.assertEqual(s.quantile(q), 7.5)

    def test_all_identical(self):
        s = GKSummary(EPS)
        s.update([3.14] * 10_000)
        for q in QUANTILES:
            self.assertEqual(s.quantile(q), 3.14)
        self.assertLessEqual(s.effective_epsilon, EPS)

    def test_min_max_exact(self):
        data = make_data("uniform", 5000)
        s = GKSummary(EPS)
        s.update(data)
        self.assertEqual(s.quantile(0.0), min(data))
        self.assertEqual(s.quantile(1.0), max(data))

    def test_invalid_args(self):
        with self.assertRaises(ValueError):
            GKSummary(0.0)
        with self.assertRaises(ValueError):
            GKSummary(0.01, max_size=1)
        s = GKSummary(EPS)
        s.add(1.0)
        with self.assertRaises(ValueError):
            s.quantile(1.5)

    def test_merge_with_empty(self):
        data = make_data("uniform", 1000)
        s = GKSummary(EPS)
        s.update(data)
        empty = GKSummary(EPS)
        s.copy().merge(empty)          # merge empty into full
        empty.merge(s)                 # merge full into empty
        self.assertEqual(empty.n, len(data))
        self.assertEqual(empty.quantile(1.0), max(data))


class TestDistributions(unittest.TestCase):
    """Reported bound must hold against exact ranks for all 4 distributions."""

    def test_all_distributions(self):
        for dist in ("uniform", "exponential", "bimodal", "duplicates"):
            with self.subTest(dist=dist):
                data = make_data(dist, N)
                s = GKSummary(EPS)
                s.update(data)
                full = sorted(data)
                self.assertLessEqual(s.rank_error_bound(), EPS * N + 2)
                for q in QUANTILES:
                    check_quantile(s, full, q)

    def test_extreme_skew_and_duplicates_low_bias(self):
        # point estimates should land far inside the (already small) bound
        for dist in ("exponential", "duplicates"):
            data = make_data(dist, N)
            s = GKSummary(EPS)
            s.update(data)
            full = sorted(data)
            for q in QUANTILES:
                _, _, actual = check_quantile(s, full, q)
                self.assertLessEqual(actual, EPS * N)


class TestMerge(unittest.TestCase):
    def test_merge_consistency(self):
        rng = random.Random(7)
        data = [rng.random() if rng.random() < 0.5
                else rng.expovariate(1.0) for _ in range(N)]
        cut = N // 2
        s1, s2 = GKSummary(EPS), GKSummary(EPS)
        s1.update(data[:cut])
        s2.update(data[cut:])
        merged = s1.copy().merge(s2)
        direct = GKSummary(EPS)
        direct.update(data)
        full = sorted(data)

        e_m, e_d = merged.rank_error_bound(), direct.rank_error_bound()
        # same order of magnitude as direct processing
        self.assertLessEqual(e_m, 4 * e_d)
        for q in QUANTILES:
            est_m, bound_m, _ = check_quantile(merged, full, q)
            est_d, bound_d, _ = check_quantile(direct, full, q)
            # merged estimate itself respects its own bound
            self.assertLessEqual(bound_m, e_m)
            # the two estimates' true ranks are close: each is within its
            # own bound of the target rank, hence within the sum of bounds
            gap = interval_distance(rank_interval(full, est_m),
                                    rank_interval(full, est_d))
            self.assertLessEqual(gap, bound_m + bound_d)

    def test_merge_many_partitions(self):
        data = make_data("uniform", 20_000)
        parts = [GKSummary(EPS) for _ in range(8)]
        for k, x in enumerate(data):
            parts[k % 8].add(x)
        merged = parts[0]
        for p in parts[1:]:
            merged = merged.copy().merge(p)
        full = sorted(data)
        self.assertEqual(merged.n, len(data))
        for q in QUANTILES:
            check_quantile(merged, full, q)


class TestMemoryBudget(unittest.TestCase):
    def test_budget_respected(self):
        data = make_data("uniform", N)
        s = GKSummary(EPS, max_size=64)
        s.update(data)
        self.assertLessEqual(s.size, 64)
        full = sorted(data)
        for q in QUANTILES:
            check_quantile(s, full, q)

    def test_tiny_budget_degrades_explicitly(self):
        data = make_data("uniform", N)
        full = sorted(data)
        plain = GKSummary(EPS)
        plain.update(data)
        tiny = GKSummary(EPS, max_size=8)
        tiny.update(data)
        self.assertTrue(tiny.degraded)
        self.assertLessEqual(tiny.size, 8)
        # degradation must be visible in the reported bound...
        self.assertGreater(tiny.rank_error_bound(),
                           plain.rank_error_bound())
        # ...and the larger bound must still be valid
        for q in QUANTILES:
            check_quantile(tiny, full, q)

    def test_unbounded_size_is_logarithmic(self):
        data = make_data("uniform", N)
        s = GKSummary(EPS)
        s.update(data)
        # GK summary size is O((1/eps) * log(eps*N)); allow generous slack
        self.assertLessEqual(s.size, (10.0 / EPS) * math.log2(EPS * N + 2))


if __name__ == "__main__":
    unittest.main()
