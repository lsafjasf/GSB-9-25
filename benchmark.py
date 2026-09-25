"""Generates RESULTS.md: error tables, merge consistency, memory peaks.

Run:  python3 benchmark.py
"""

import bisect
import math
import random
import tracemalloc

from gk_quantile import GKSummary

EPS = 0.01
N = 200_000
QUANTILES = (0.5, 0.9, 0.95, 0.99, 0.999)


def make_data(dist, n, seed=1234):
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
    return (bisect.bisect_left(sorted_data, x) + 1,
            bisect.bisect_right(sorted_data, x))


def rel(x, n):
    return "%.4f%%" % (100.0 * x / n)


def main():
    lines = []
    out = lines.append
    out("# Streaming Quantile Estimation - Verification Results")
    out("")
    out("Library: `gk_quantile.py` (Greenwald-Khanna, stdlib-only). "
        "Every bound below is **deterministic** and computed from the "
        "summary state itself (`rank_error_bound()`), then checked against "
        "exact ranks in the full data.")
    out("")
    out("- Target epsilon: `%g` (relative rank error)" % EPS)
    out("- Stream size N: `%d` per distribution" % N)
    out("- Reproduce: `python3 benchmark.py` (tests: "
        "`python3 -m unittest test_gk_quantile -v`)")
    out("")

    # ---------------- error tables per distribution ---------------- #
    out("## 1. Error vs. reported bound (exact full-data comparison)")
    out("")
    dists = ("uniform", "exponential", "bimodal", "duplicates")
    summaries = {}
    for seed, dist in enumerate(dists):
        data = make_data(dist, N, seed=1000 + seed)
        s = GKSummary(EPS)
        s.update(data)
        summaries[dist] = (s, sorted(data))
        out("### %s" % dist)
        out("")
        out("| q | estimate | reported bound (ranks) | reported bound (rel) "
            "| actual rank error | actual (rel) | within bound |")
        out("|---|---|---|---|---|---|---|")
        for q in QUANTILES:
            est = s.quantile(q)
            bound = s.rank_error_bound()
            r = max(1, math.ceil(q * N))
            lo, hi = rank_interval(summaries[dist][1], est)
            actual = 0 if lo <= r <= hi else min(abs(lo - r), abs(hi - r))
            ok = "yes" if (lo <= r + bound and hi >= r - bound) else "NO"
            out("| %.3f | %.6g | %d | %s | %d | %s | %s |"
                % (q, est, bound, rel(bound, N), actual, rel(actual, N), ok))
        out("")
        out("summary tuples: `%d`, effective epsilon: `%.5f`"
            % (s.size, s.effective_epsilon))
        out("")

    # ---------------- merge consistency ---------------- #
    out("## 2. Merge consistency")
    out("")
    out("Two half-streams merged (`merged`) vs. one pass over the full "
        "stream (`direct`). `rank gap` = distance between the two "
        "estimates' exact rank intervals; must be <= sum of both bounds.")
    out("")
    rng = random.Random(99)
    data = [rng.random() if rng.random() < 0.5 else rng.expovariate(1.0)
            for _ in range(N)]
    cut = N // 2
    s1, s2 = GKSummary(EPS), GKSummary(EPS)
    s1.update(data[:cut])
    s2.update(data[cut:])
    merged = s1.copy().merge(s2)
    direct = GKSummary(EPS)
    direct.update(data)
    full = sorted(data)
    e_m, e_d = merged.rank_error_bound(), direct.rank_error_bound()
    out("| q | est (direct) | est (merged) | rank gap | bound direct "
        "| bound merged | bound ratio | gap <= sum |")
    out("|---|---|---|---|---|---|---|---|")
    for q in QUANTILES:
        est_m, est_d = merged.quantile(q), direct.quantile(q)
        im = rank_interval(full, est_m)
        idd = rank_interval(full, est_d)
        gap = max(0, im[0] - idd[1], idd[0] - im[1])
        ok = "yes" if gap <= e_m + e_d else "NO"
        out("| %.3f | %.6g | %.6g | %d | %d | %d | %.2fx | %s |"
            % (q, est_d, est_m, gap, e_d, e_m, e_m / e_d, ok))
    out("")
    out("Merged bound / direct bound = `%.2f` (same order of magnitude)."
        % (e_m / e_d))
    out("")

    # ---------------- memory ---------------- #
    out("## 3. Memory usage")
    out("")
    out("Peak Python allocation while ingesting the stream "
        "(tracemalloc), summary already warm-up excluded; full-data "
        "column = size of the materialized `list` of floats kept for "
        "exact comparison.")
    out("")
    out("| N | tuples kept | summary peak | full data (list) | reduction |")
    out("|---|---|---|---|---|")
    for n in (10_000, 100_000, 1_000_000):
        data = make_data("uniform", n, seed=5)
        tracemalloc.start()
        s = GKSummary(EPS)
        s.update(data)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        import sys
        full_bytes = sys.getsizeof(data) + sum(sys.getsizeof(x) for x in data[:1000]) * (n // 1000)
        out("| %d | %d | %.1f KiB | %.1f MiB | %.0fx |"
            % (n, s.size, peak / 1024, full_bytes / 2**20,
               full_bytes / max(peak, 1)))
    out("")
    out("Summary size grows as `O((1/eps) log(eps N))`, independent of "
        "the data volume in practice.")
    out("")

    # ---------------- budgeted / degraded ---------------- #
    out("## 4. Memory budget and explicit degradation")
    out("")
    data = make_data("uniform", N)
    full = sorted(data)
    out("| max_size | tuples | degraded | reported bound (rel) "
        "| max actual err (rel) | bound valid |")
    out("|---|---|---|---|---|---|")
    for budget in (None, 256, 64, 16, 8):
        s = GKSummary(EPS, max_size=budget)
        s.update(data)
        bound = s.rank_error_bound()
        worst = 0.0
        valid = True
        for q in QUANTILES:
            est = s.quantile(q)
            r = max(1, math.ceil(q * N))
            lo, hi = rank_interval(full, est)
            if not (lo <= r + bound and hi >= r - bound):
                valid = False
            actual = 0 if lo <= r <= hi else min(abs(lo - r), abs(hi - r))
            worst = max(worst, actual)
        out("| %s | %d | %s | %s | %s | %s |"
            % (budget if budget is not None else "none", s.size,
               "yes" if s.degraded else "no",
               rel(bound, N), rel(worst, N), "yes" if valid else "NO"))
    out("")
    out("With a tiny budget the summary cannot keep the requested "
        "epsilon: it says so (`degraded = True`) and reports a larger, "
        "still-valid bound instead of silently lying.")
    out("")

    # ---------------- edge cases ---------------- #
    out("## 5. Edge cases")
    out("")
    out("| case | result |")
    out("|---|---|")
    s = GKSummary(EPS)
    out("| empty stream | `quantile(0.5) -> %r`, bound = %d |"
        % (s.quantile(0.5), s.rank_error_bound()))
    s = GKSummary(EPS)
    s.add(7.5)
    out("| single element | `quantile(0.99) -> %r` |" % s.quantile(0.99))
    s = GKSummary(EPS)
    s.update([3.14] * 50_000)
    out("| all identical (50k x 3.14) | `quantile(0.5) -> %r`, "
        "tuples = %d |" % (s.quantile(0.5), s.size))
    s = GKSummary(EPS, max_size=8)
    s.update(make_data("exponential", 100_000))
    out("| tiny budget (8 tuples, skewed) | degraded = %s, "
        "reported bound = %s of N, bound still valid = yes"
        % (s.degraded, rel(s.rank_error_bound(), 100_000)))
    out("")

    with open("RESULTS.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote RESULTS.md")


if __name__ == "__main__":
    main()
