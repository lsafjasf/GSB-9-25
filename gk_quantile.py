"""Streaming quantile estimation with deterministic error bounds.

Implements a Greenwald-Khanna (GK) style epsilon-approximate quantile
summary with:

  * single-pass, fixed-memory streaming (`add`)
  * a configurable hard memory budget (`max_size`) with explicit,
    honest degradation (the reported error bound grows)
  * deterministic, verifiable rank-error bounds (`rank_error_bound`)
  * mergeable summaries (`merge`) for distributed/parallel streams

Standard library only.

Guarantee (deterministic, not probabilistic):
    For any rank r in [1, n], the value returned for rank r has its true
    rank in the stream within +/- rank_error_bound() of r.
"""

import bisect
import math

_MIN_BUDGET = 2
_SAFETY = 4  # internal epsilon = requested epsilon / _SAFETY


class GKSummary:
    """Epsilon-approximate quantile summary for one stream.

    Parameters:
        epsilon: target relative rank error (e.g. 0.01 -> ranks within 1%).
        max_size: optional hard cap on the number of summary tuples kept.
            If the cap is too small for the requested epsilon, the summary
            degrades: it raises its internal epsilon and reports a larger
            (still valid) error bound. `degraded` becomes True.
    """

    __slots__ = ("_eps", "_max_size", "_n", "_tuples", "_degraded",
                 "_since_compress")

    def __init__(self, epsilon=0.01, max_size=None):
        if not 0.0 < epsilon < 1.0:
            raise ValueError("epsilon must be in (0, 1)")
        if max_size is not None and max_size < _MIN_BUDGET:
            raise ValueError("max_size must be >= %d" % _MIN_BUDGET)
        self._eps = epsilon / _SAFETY
        self._max_size = max_size
        self._n = 0
        self._tuples = []  # list of [value, g, delta], sorted by value
        self._degraded = False
        self._since_compress = 0

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #
    @property
    def n(self):
        """Number of stream elements observed."""
        return self._n

    @property
    def size(self):
        """Number of tuples currently held (memory footprint proxy)."""
        return len(self._tuples)

    @property
    def degraded(self):
        """True if the memory budget forced a larger effective epsilon."""
        return self._degraded

    @property
    def effective_epsilon(self):
        """Current guaranteed relative rank error: rank_error_bound()/n."""
        if self._n == 0:
            return 0.0
        return self.rank_error_bound() / self._n

    def __len__(self):
        return self._n

    # ------------------------------------------------------------------ #
    # streaming
    # ------------------------------------------------------------------ #
    def add(self, x):
        """Observe one value. O(summary size)."""
        t = self._tuples
        lo, hi = 0, len(t)
        while lo < hi:
            mid = (lo + hi) // 2
            if x < t[mid][0]:
                hi = mid
            else:
                lo = mid + 1
        if lo == 0 or lo == len(t):
            delta = 0  # keep stream min/max exact
        else:
            delta = math.floor(2.0 * self._eps * self._n)
        t.insert(lo, [x, 1, delta])
        self._n += 1
        self._since_compress += 1
        period = max(1, int(1.0 / (2.0 * self._eps)))
        if (self._since_compress >= period
                or (self._max_size is not None and len(t) > self._max_size)):
            self._compress()
            self._since_compress = 0
            self._enforce_budget()

    def update(self, iterable):
        for x in iterable:
            self.add(x)

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    def quantile(self, q):
        """Estimate the q-quantile. Returns None on an empty stream."""
        if not 0.0 <= q <= 1.0:
            raise ValueError("q must be in [0, 1]")
        if self._n == 0:
            return None
        r = max(1, int(math.ceil(q * self._n)))
        return self._value_at_rank(r)

    def rank_error_bound(self):
        """Deterministic additive rank-error bound, in rank units.

        For any rank r, the value returned for r has true rank within
        +/- this bound of r. Computed exactly from the current state,
        so it stays honest after merges and memory-budget degradation.
        """
        t = self._tuples
        if not t:
            return 0
        max_gd = 0
        max_d = 0
        for _, g, d in t:
            if g + d > max_gd:
                max_gd = g + d
            if d > max_d:
                max_d = d
        return max_gd + max_d

    def quantile_with_bound(self, q):
        """Return (estimate, rank_error_bound) for the q-quantile."""
        return self.quantile(q), self.rank_error_bound()

    def quantile_interval(self, q):
        """Return (estimate, lo, hi): the true q-quantile lies in [lo, hi]."""
        if self._n == 0:
            return None, None, None
        r = max(1, int(math.ceil(q * self._n)))
        e = self.rank_error_bound()
        lo = self._value_at_rank(max(1, r - e))
        hi = self._value_at_rank(min(self._n, r + e))
        return self._value_at_rank(r), lo, hi

    def _value_at_rank(self, r):
        t = self._tuples
        g_prefix = 0
        chosen = t[0][0]
        for v, g, d in t:
            g_prefix += g
            if g_prefix - d <= r:
                chosen = v
            else:
                break
        return chosen

    # ------------------------------------------------------------------ #
    # merging
    # ------------------------------------------------------------------ #
    def merge(self, other):
        """Merge another summary into this one (self is updated).

        The result summarizes the union of both streams. Its error bound
        remains valid and stays the same order of magnitude as a summary
        built directly over the concatenated stream.
        """
        if not isinstance(other, GKSummary):
            raise TypeError("can only merge another GKSummary")
        if other._n == 0:
            return self
        if self._n == 0:
            self._eps = other._eps
            self._n = other._n
            self._tuples = [t[:] for t in other._tuples]
            self._degraded = self._degraded or other._degraded
            self._enforce_budget()
            return self

        meta_a = _meta(self._tuples)
        meta_b = _meta(other._tuples)
        a, b = self._tuples, other._tuples
        merged = []
        i = j = 0
        while i < len(a) or j < len(b):
            if j >= len(b) or (i < len(a) and a[i][0] <= b[j][0]):
                v, g, d = a[i]
                rmin_s, rmax_s = meta_a[3][i], meta_a[4][i]
                lo_o, hi_o = _count_bounds(meta_b, v, other._n)
                i += 1
            else:
                v, g, d = b[j]
                rmin_s, rmax_s = meta_b[3][j], meta_b[4][j]
                lo_o, hi_o = _count_bounds(meta_a, v, self._n)
                j += 1
            merged.append([v, g, rmin_s + lo_o, rmax_s + hi_o])

        out = []
        g_prefix = 0
        for v, g, rmin, rmax in merged:
            g_prefix += g
            delta = max(g_prefix - rmin, rmax - g_prefix, 0)
            out.append([v, g, delta])

        self._tuples = out
        self._n += other._n
        self._eps = max(self._eps, other._eps)
        self._degraded = self._degraded or other._degraded
        self._compress()
        self._enforce_budget()
        return self

    def copy(self):
        s = GKSummary.__new__(GKSummary)
        s._eps = self._eps
        s._max_size = self._max_size
        s._n = self._n
        s._tuples = [t[:] for t in self._tuples]
        s._degraded = self._degraded
        s._since_compress = self._since_compress
        return s

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _compress(self):
        t = self._tuples
        threshold = math.floor(2.0 * self._eps * self._n)
        i = len(t) - 2
        while i >= 1:
            if t[i][1] + t[i + 1][1] + t[i + 1][2] <= threshold:
                t[i + 1][1] += t[i][1]
                del t[i]
            i -= 1

    def _enforce_budget(self):
        if self._max_size is None:
            return
        while len(self._tuples) > self._max_size:
            before = len(self._tuples)
            self._eps *= 1.5
            self._degraded = True
            self._compress()
            if len(self._tuples) == before:
                self._force_compress()
                if len(self._tuples) == before:
                    break

    def _force_compress(self):
        t = self._tuples
        i = len(t) - 2
        while i >= 1:
            t[i + 1][1] += t[i][1]
            del t[i]
            i -= 2


def _meta(tuples):
    """Precompute per-tuple rank metadata for merge lookups."""
    vals = []
    rmins = []
    rmaxs = []
    prefix_max_rmin = []
    g_prefix = 0
    best = 0
    for v, g, d in tuples:
        g_prefix += g
        vals.append(v)
        rmin = g_prefix - d
        rmax = g_prefix + d
        rmins.append(rmin)
        rmaxs.append(rmax)
        if rmin > best:
            best = rmin
        prefix_max_rmin.append(best)
    suffix_min = [math.inf] * (len(tuples) + 1)
    for k in range(len(tuples) - 1, -1, -1):
        suffix_min[k] = min(suffix_min[k + 1], rmaxs[k] - tuples[k][1])
    return vals, prefix_max_rmin, suffix_min, rmins, rmaxs


def _count_bounds(meta, v, n_other):
    """Bounds on how many of the other stream's elements relate to v.

    Returns (lo, hi): a lower bound on count(< v) and an upper bound on
    count(<= v) in the other stream, derived from its summary.
    """
    vals, prefix_max_rmin, suffix_min, _, rmaxs = meta
    k_left = bisect.bisect_left(vals, v)
    k_right = bisect.bisect_right(vals, v)
    lo = prefix_max_rmin[k_left - 1] if k_left > 0 else 0
    hi = suffix_min[k_right]
    if k_right > k_left:
        hi = min(hi, rmaxs[k_right - 1])
    return lo, min(hi, n_other)
