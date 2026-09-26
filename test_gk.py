"""test_gk.py -- self-tests and benchmarks for gk.GKSummary. Stdlib only.

Run:  python3 test_gk.py
Exit code 0 = all guarantees verified; non-zero = a guarantee was violated.

Covers:
  1. Accuracy vs exact quantiles on 4 distributions (uniform, exponential,
     bimodal, heavy duplicates) with a verifiable rank-error bound.
  2. Merge consistency: merged(stream A, stream B) vs direct(A + B).
  3. Edge cases: empty stream, single element, all-identical values,
     extremely small memory budget (explicit degradation).
  4. Memory: tracemalloc peak while streaming, summary size, bytes.
"""

import bisect
import math
import random
import sys
import tracemalloc

from gk import GKSummary

QUANTILES = [0.5, 0.9, 0.95, 0.99]
FAILURES = []


# ------------------------------------------------------------- distributions

def gen_uniform(n, rng):
    return [rng.random() for _ in range(n)]

def gen_exponential(n, rng):
    return [rng.expovariate(1.0) for _ in range(n)]

def gen_bimodal(n, rng):
    return [rng.gauss(0.0, 1.0) if rng.random() < 0.5 else rng.gauss(10.0, 1.0)
            for _ in range(n)]

def gen_duplicates(n, rng):
    # 90% of the stream is the exact same value: worst case for ties.
    return [1.0 if rng.random() < 0.9 else rng.random() for _ in range(n)]

DISTRIBUTIONS = [
    ("uniform",     gen_uniform),
    ("exponential", gen_exponential),
    ("bimodal",     gen_bimodal),
    ("duplicates",  gen_duplicates),
]


# ------------------------------------------------------------------ helpers

def rank_error(sorted_data, q, estimate):
    """True distance between ceil(q*n) and the rank range of `estimate`."""
    n = len(sorted_data)
    r = max(1, math.ceil(q * n))
    lo = bisect.bisect_left(sorted_data, estimate) + 1   # 1-based first rank
    hi = bisect.bisect_right(sorted_data, estimate)      # 1-based last rank
    if lo <= r <= hi:
        return 0
    return lo - r if r < lo else r - hi

def exact_quantile(sorted_data, q):
    n = len(sorted_data)
    return sorted_data[max(1, math.ceil(q * n)) - 1]

def check(ok, label):
    if not ok:
        FAILURES.append(label)
        print("  FAIL: %s" % label)

def fmt(x):
    return "%.6g" % x if isinstance(x, float) else str(x)


# ------------------------------------------------------- 1. accuracy tables

def test_accuracy(n=100_000, epsilon=0.01):
    print("=" * 88)
    print("1. ACCURACY vs EXACT  (n=%d, epsilon=%.3f, bound = ceil(eps_eff*n) ranks)"
          % (n, epsilon))
    print("=" * 88)
    for name, gen in DISTRIBUTIONS:
        rng = random.Random(20260926)
        data = gen(n, rng)
        s = GKSummary(epsilon=epsilon)
        s.update(data)
        exact_sorted = sorted(data)
        bound = s.error_bound()
        print("\n[%s]  summary: %s" % (name, s))
        print("  %-6s | %-12s | %-12s | %-10s | %-10s | %s"
              % ("q", "exact", "estimate", "rank_err", "bound", "ok"))
        print("  " + "-" * 66)
        worst = 0
        for q in QUANTILES:
            est, b = s.quantile_with_bound(q)
            err = rank_error(exact_sorted, q, est)
            worst = max(worst, err)
            ok = err <= b
            check(ok, "accuracy %s q=%.2f err=%d bound=%d" % (name, q, err, b))
            print("  %-6.2f | %-12s | %-12s | %-10d | %-10d | %s"
                  % (q, fmt(exact_quantile(exact_sorted, q)), fmt(est),
                     err, b, "OK" if ok else "VIOLATED"))
        print("  worst rank error: %d ranks (%.4f%% of n), bound: %d (%.2f%% of n)"
              % (worst, 100.0 * worst / n, bound, 100.0 * bound / n))


# ------------------------------------------------------- 2. merge consistency

def test_merge(n=60_000, epsilon=0.01):
    print("\n" + "=" * 88)
    print("2. MERGE CONSISTENCY  (two halves of n=%d merged vs direct, epsilon=%.3f)"
          % (n, epsilon))
    print("=" * 88)
    print("  %-12s | %-12s | %-12s | %-12s | %s"
          % ("dist", "direct_err", "merged_err", "merged_bound", "ok"))
    print("  " + "-" * 70)
    for name, gen in DISTRIBUTIONS:
        rng = random.Random(7654321)
        data = gen(n, rng)
        half = n // 2
        s1, s2 = GKSummary(epsilon), GKSummary(epsilon)
        s1.update(data[:half])
        s2.update(data[half:])
        merged = s1.merged(s2)
        direct = GKSummary(epsilon)
        direct.update(data)
        exact_sorted = sorted(data)
        d_err = max(rank_error(exact_sorted, q, direct.quantile(q)) for q in QUANTILES)
        m_err = max(rank_error(exact_sorted, q, merged.quantile(q)) for q in QUANTILES)
        bound = merged.error_bound()
        ok_bound = m_err <= bound
        # "same order of magnitude": allow 4x slack plus a small absolute floor.
        ok_order = m_err <= max(4 * d_err, math.ceil(0.005 * n))
        check(ok_bound, "merge %s: merged err %d > bound %d" % (name, m_err, bound))
        check(ok_order, "merge %s: merged err %d not same order as direct %d"
              % (name, m_err, d_err))
        print("  %-12s | %-12d | %-12d | %-12d | %s"
              % (name, d_err, m_err, bound,
                 "OK" if (ok_bound and ok_order) else "VIOLATED"))


# ------------------------------------------------------------ 3. edge cases

def test_edge_cases():
    print("\n" + "=" * 88)
    print("3. EDGE CASES")
    print("=" * 88)

    # empty stream
    s = GKSummary(0.01)
    est, b = s.quantile_with_bound(0.5)
    check(est is None and b == 0, "empty stream must yield (None, 0)")
    print("  empty stream:        quantile(0.5) -> (None, 0)            OK")

    # single element
    s = GKSummary(0.01)
    s.add(42.0)
    ok = all(s.quantile(q) == 42.0 for q in [0.0, 0.5, 0.99, 1.0])
    check(ok, "single element must answer every quantile exactly")
    print("  single element:      every quantile -> 42.0                OK")

    # all identical values
    s = GKSummary(0.01)
    s.update([7.0] * 10_000)
    ok = (s.quantile(0.5) == 7.0 and s.quantile(0.99) == 7.0
          and s.summary_size <= 3)
    check(ok, "all-identical stream must stay tiny and exact")
    print("  all identical:       quantiles -> 7.0, entries=%d          OK"
          % s.summary_size)

    # extremely small memory budget: must degrade explicitly
    n = 100_000
    rng = random.Random(999)
    data = [rng.expovariate(1.0) for _ in range(n)]   # skewed
    s = GKSummary(epsilon=0.001, max_entries=8)
    s.update(data)
    exact_sorted = sorted(data)
    bound = s.error_bound()
    errs = [rank_error(exact_sorted, q, s.quantile(q)) for q in QUANTILES]
    ok = (s.summary_size <= 8 and s.degraded and s.eps_eff > 0.001
          and all(e <= bound for e in errs))
    check(ok, "tiny budget: entries<=8, degraded, errors within enlarged bound")
    print("  tiny budget (max_entries=8, requested eps=0.001, n=%d):" % n)
    print("    -> degraded: eps_eff=%.4f, entries=%d, bound=%d ranks (%.2f%% of n)"
          % (s.eps_eff, s.summary_size, bound, 100.0 * bound / n))
    print("    -> actual rank errors at q=%s: %s  (all <= bound: %s)"
          % (QUANTILES, errs, all(e <= bound for e in errs)))


# ------------------------------------------------------------------ 4. memory

def test_memory(n=200_000, epsilon=0.01):
    print("\n" + "=" * 88)
    print("4. MEMORY  (streaming n=%d floats; tracemalloc peak of the summary)" % n)
    print("=" * 88)
    print("  %-34s | %-8s | %-10s | %-12s | %s"
          % ("scenario", "entries", "eps_eff", "peak(bytes)", "bytes/item"))
    print("  " + "-" * 82)
    scenarios = [("eps=0.01, no cap", epsilon, None),
                 ("eps=0.01, max_entries=64", epsilon, 64),
                 ("eps=0.001, max_entries=32", 0.001, 32)]
    for label, eps, cap in scenarios:
        rng = random.Random(31337)
        s = GKSummary(epsilon=eps, max_entries=cap)
        tracemalloc.start()
        for _ in range(n):
            s.add(rng.random())
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if cap is not None:
            check(s.summary_size <= cap, "memory cap %d violated" % cap)
        print("  %-34s | %-8d | %-10.4f | %-12d | %.4f"
              % (label, s.summary_size, s.eps_eff, peak, peak / n))
    exact_bytes = n * 8  # array('d') of doubles, the cheapest exact storage
    print("  %-34s | %-8s | %-10s | %-12d | %.1f"
          % ("exact storage (all n doubles)", n, "-", exact_bytes, 8.0))


# --------------------------------------------------------------------- main

if __name__ == "__main__":
    test_accuracy()
    test_merge()
    test_edge_cases()
    test_memory()
    print("\n" + "=" * 88)
    if FAILURES:
        print("RESULT: %d FAILURE(S)" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("RESULT: ALL TESTS PASSED -- every reported error bound was honored.")
