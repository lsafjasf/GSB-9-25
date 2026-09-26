"""backend_selector: deterministic backend selection library (stdlib only).

Strategies
----------
- WeightedRoundRobin: smooth weighted round robin (nginx-style SWRR).
- LeastConnections:   weighted least in-flight connections.

Common semantics
----------------
- A node is *eligible* iff it is alive (not drained by health checks) and
  its weight > 0. Ineligible nodes are never selected.
- If no node is eligible, select() raises NoAvailableBackendError.
- All selections are deterministic: given the same sequence of operations
  (add/remove/set_weight/set_alive/select/release), the sequence of
  returned node ids is identical across runs and processes.
- Tie-breaking (both strategies): among candidates that compare equal by
  the strategy key, the node with the *smallest* id (natural ordering of
  the id type) wins. Node ids must therefore be mutually comparable.

Recovery bound (WeightedRoundRobin)
-----------------------------------
When a node with weight w is restored (set_alive(True) or weight raised
from 0) and the total effective weight is W, the node is selected again
within at most ceil(W / w) select() calls. Proof sketch: in SWRR every
eligible node's current weight grows by its own weight each round and the
winner's drops by W, so all current weights stay within (-W, W]. A
restored node starts at <= 0 and gains w per round; after k rounds its
current weight is >= k*w - 0 while any rival's is < W, so it must win a
round once k*w >= W, i.e. k = ceil(W / w).
"""

from __future__ import annotations

import threading
from collections import Counter
from contextlib import contextmanager


class NoAvailableBackendError(RuntimeError):
    """Raised when no node is eligible for selection."""


class _Node:
    __slots__ = ("id", "weight", "alive", "current", "in_flight")

    def __init__(self, node_id, weight):
        self.id = node_id
        self.weight = weight
        self.alive = True
        self.current = 0  # SWRR current weight
        self.in_flight = 0  # least-connections in-flight count


class BaseSelector:
    """Unified interface: add/remove/set_weight/set_alive/select/stats."""

    def __init__(self):
        self._nodes = {}
        self._lock = threading.Lock()
        self._counts = Counter()

    # -- membership -----------------------------------------------------
    def add(self, node_id, weight=1):
        if weight < 0:
            raise ValueError("weight must be >= 0")
        with self._lock:
            if node_id in self._nodes:
                raise ValueError(f"duplicate node id: {node_id!r}")
            self._nodes[node_id] = _Node(node_id, weight)

    def remove(self, node_id):
        with self._lock:
            del self._nodes[node_id]

    def set_weight(self, node_id, weight):
        if weight < 0:
            raise ValueError("weight must be >= 0")
        with self._lock:
            self._nodes[node_id].weight = weight

    def set_alive(self, node_id, alive):
        with self._lock:
            self._nodes[node_id].alive = bool(alive)

    # -- selection ------------------------------------------------------
    def select(self):
        raise NotImplementedError

    def _eligible(self):
        return [n for n in self._nodes.values() if n.alive and n.weight > 0]

    # -- observability --------------------------------------------------
    def stats(self):
        """Return {node_id: (count, share)} over all successful selects."""
        with self._lock:
            total = sum(self._counts.values())
            return {
                nid: (c, (c / total) if total else 0.0)
                for nid, c in sorted(self._counts.items())
            }

    def total_selected(self):
        with self._lock:
            return sum(self._counts.values())


class WeightedRoundRobin(BaseSelector):
    """Smooth weighted round robin. Tie-break: smallest node id."""

    def select(self):
        with self._lock:
            candidates = self._eligible()
            if not candidates:
                raise NoAvailableBackendError("no eligible backend")
            total = sum(n.weight for n in candidates)
            for n in candidates:
                n.current += n.weight
            # min by (-current, id): highest current weight, ties -> smallest id
            best = min(candidates, key=lambda n: (-n.current, n.id))
            best.current -= total
            self._counts[best.id] += 1
            return best.id


class LeastConnections(BaseSelector):
    """Weighted least connections.

    Picks the eligible node minimizing in_flight / weight (a node with
    twice the weight is expected to hold twice the connections).
    Tie-break: smallest node id. select() increments the chosen node's
    in-flight count; call release() (or use the connection() context
    manager) when the request finishes.
    """

    def select(self):
        with self._lock:
            candidates = self._eligible()
            if not candidates:
                raise NoAvailableBackendError("no eligible backend")
            # key: in_flight/weight without floats -> in_flight * other weight
            best = min(
                candidates,
                key=lambda n: (n.in_flight / n.weight, n.id),
            )
            best.in_flight += 1
            self._counts[best.id] += 1
            return best.id

    def release(self, node_id):
        with self._lock:
            node = self._nodes[node_id]
            if node.in_flight <= 0:
                raise ValueError(f"node {node_id!r} has no in-flight request")
            node.in_flight -= 1

    @contextmanager
    def connection(self):
        node_id = self.select()
        try:
            yield node_id
        finally:
            self.release(node_id)

    def in_flight(self, node_id=None):
        with self._lock:
            if node_id is not None:
                return self._nodes[node_id].in_flight
            return {nid: n.in_flight for nid, n in self._nodes.items()}
