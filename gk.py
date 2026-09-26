"""gk.py -- Greenwald-Khanna (GK) streaming quantile estimation. Stdlib only.

Guarantee (deterministic, verifiable):
    For any quantile q in [0, 1], the value v returned by quantile(q) satisfies

        | rank(v) - ceil(q * n) | <= error_bound() = ceil(eps_eff * n)

    where rank(v) is the true rank of v in the full stream (any position within
    the run of equal values counts), n is the number of observed items, and
    eps_eff is the *effective* epsilon (it grows only if the configured memory
    budget forces a degradation; it is always inspectable and reported).

Memory:
    The summary holds at most max_entries tuples (if configured). When the
    budget would be exceeded, eps_eff is increased (degradation) and the
    summary is re-compressed, so the memory upper bound is hard while the
    error bound grows accordingly and is always honestly reported.

Merging:
    merge() combines two summaries. If the inputs have errors <= e1*n1 and
    e2*n2, the merged summary has error <= max(e1, e2) * (n1 + n2), i.e. the
    same order as processing the concatenated stream directly.
"""

import bisect
import math
import sys

__all__ = ["GKSummary"]


class GKSummary:
    """Epsilon-approximate streaming quantile summary (Greenwald-Khanna).

    Parameters:
        epsilon:     target relative rank error, 0 < epsilon < 1.
        max_entries: optional hard cap on the number of summary tuples.
                     If the cap would be exceeded, eps_eff is increased
                     (explicit degradation) so the cap always holds.
    """

    def __init__(self, epsilon=0.01, max_entries=None):
        if not 0.0 < epsilon < 1.0:
            raise ValueError("epsilon must be in (0, 1)")
        if max_entries is not None and max_entries < 2:
            raise ValueError("max_entries must be >= 2 (min and max are kept)")
        self.epsilon = float(epsilon)      # requested accuracy
        self.eps_eff = float(epsilon)      # effective accuracy (>= epsilon)
        self.max_entries = max_entries
        self.n = 0
        self.degraded = False              # True once eps_eff was increased
        # Each entry is [value, g, delta]; _vals mirrors entry values for bisect.
        self._entries = []
        self._vals = []
        self._compress_every = max(1, int(1.0 / (2.0 * self.eps_eff)))

    # ------------------------------------------------------------------ API

    def add(self, x):
        """Observe one value. O(summary size) worst case."""
        x = float(x)
        self.n += 1
        entries, vals = self._entries, self._vals
        if not entries or x < vals[0]:
            self._insert(0, x, 1, 0)               # new minimum: exact
        elif x > vals[-1]:
            self._insert(len(entries), x, 1, 0)    # new maximum: exact
        elif x == vals[0]:
            entries[0][1] += 1                     # duplicate of minimum
        elif x == vals[-1]:
            entries[-1][1] += 1                    # duplicate of maximum
        else:
            i = bisect.bisect_right(vals, x)
            delta = int(2.0 * self.eps_eff * self.n)
            self._insert(i, x, 1, delta)
        if self.n % self._compress_every == 0:
            self._compress()
        self._enforce_budget()

    def update(self, values):
        """Observe many values."""
        for v in values:
            self.add(v)

    def quantile(self, q):
        """Return the estimated q-quantile value (None on an empty stream)."""
        return self.quantile_with_bound(q)[0]

    def quantile_with_bound(self, q):
        """Return (estimate, rank_error_bound).

        The estimate's true rank is guaranteed to be within
        rank_error_bound of ceil(q * n). Returns (None, 0) when empty.
        """
        if not 0.0 <= q <= 1.0:
            raise ValueError("q must be in [0, 1]")
        entries = self._entries
        if not entries:
            return None, 0
        if q <= 0.0:
            return entries[0][0], self.error_bound()
        if q >= 1.0:
            return entries[-1][0], self.error_bound()
        r = max(1, math.ceil(q * self.n))
        slack = self.eps_eff * self.n
        rmin = 0
        for v, g, delta in entries:
            rmin += g
            rmax = rmin + delta
            if r - rmin <= slack and rmax - r <= slack:
                return v, self.error_bound()
        return entries[-1][0], self.error_bound()

    def error_bound(self):
        """Current guaranteed rank-error bound: ceil(eps_eff * n)."""
        if self.n == 0:
            return 0
        return max(1, math.ceil(self.eps_eff * self.n))

    def merge(self, other):
        """Merge `other` into this summary (mutates and returns self).

        Error after merge is bounded by max(eps_eff) * (n1 + n2), the same
        order as a summary built over the concatenated stream.
        """
        if not isinstance(other, GKSummary):
            raise TypeError("can only merge another GKSummary")
        if other.n == 0:
            return self
        if self.n == 0:
            self._adopt(other)
            return self

        # Uncertainty contributed by the *other* summary at any value is at
        # most 2*eps*n (its max delta). Charge it to each imported tuple so
        # the GK invariant max(delta) <= 2*eps_eff*n_total still holds.
        eps = max(self.eps_eff, other.eps_eff)
        n_total = self.n + other.n
        extra_self = int(2.0 * other.eps_eff * other.n)
        extra_other = int(2.0 * self.eps_eff * self.n)

        combined = [[v, g, d + extra_self] for v, g, d in self._entries]
        combined += [[v, g, d + extra_other] for v, g, d in other._entries]
        combined.sort(key=lambda e: e[0])
        combined[0][2] = 0                          # global minimum: exact
        combined[-1][2] = 0                         # global maximum: exact

        self._entries = combined
        self._vals = [e[0] for e in combined]
        self.n = n_total
        self.eps_eff = eps
        self._compress_every = max(1, int(1.0 / (2.0 * self.eps_eff)))
        self._compress()
        self._enforce_budget()
        self._sync_eps_with_deltas()
        return self

    def merged(self, other):
        """Return a new summary merging self and other (inputs untouched)."""
        import copy
        return copy.deepcopy(self).merge(other)

    @property
    def summary_size(self):
        """Number of tuples currently held (the memory footprint driver)."""
        return len(self._entries)

    def memory_bytes(self):
        """Approximate bytes held by the summary structure."""
        total = sys.getsizeof(self._entries) + sys.getsizeof(self._vals)
        for e in self._entries:
            total += sys.getsizeof(e) + sys.getsizeof(e[0])
        return total

    # ------------------------------------------------------------- internal

    def _insert(self, i, v, g, d):
        self._entries.insert(i, [v, g, d])
        self._vals.insert(i, v)

    def _adopt(self, other):
        import copy
        self.epsilon = other.epsilon
        self.eps_eff = other.eps_eff
        self.max_entries = self.max_entries
        self.n = other.n
        self.degraded = other.degraded
        self._entries = copy.deepcopy(other._entries)
        self._vals = list(other._vals)
        self._compress_every = other._compress_every

    def _compress(self):
        """Merge adjacent tuples while preserving the error invariant."""
        entries, vals = self._entries, self._vals
        if len(entries) <= 2:
            return
        threshold = int(2.0 * self.eps_eff * self.n)
        i = len(entries) - 2
        while i >= 1:                               # never remove min (index 0)
            e, nxt = entries[i], entries[i + 1]
            if e[1] + nxt[1] + nxt[2] <= threshold:
                nxt[1] += e[1]
                del entries[i]
                del vals[i]
            i -= 1

    def _enforce_budget(self):
        """Hard memory cap: degrade eps_eff until the summary fits."""
        if self.max_entries is None:
            return
        limit = max(2, self.max_entries)
        while len(self._entries) > limit:
            self.eps_eff *= 1.25
            self.degraded = True
            self._compress_every = max(1, int(1.0 / (2.0 * self.eps_eff)))
            before = len(self._entries)
            self._compress()
            if len(self._entries) >= before:
                # Cannot shrink further (only min/max left); make eps honest.
                self._sync_eps_with_deltas()
                break

    def _sync_eps_with_deltas(self):
        """Ensure eps_eff >= max(delta) / (2n) so the GK guarantee holds."""
        if not self._entries or self.n == 0:
            return
        max_delta = max(e[2] for e in self._entries)
        needed = max_delta / (2.0 * self.n)
        if needed > self.eps_eff:
            self.eps_eff = needed
            self.degraded = True

    def __repr__(self):
        return ("GKSummary(n=%d, eps_eff=%.5f, entries=%d, degraded=%s)"
                % (self.n, self.eps_eff, len(self._entries), self.degraded))
