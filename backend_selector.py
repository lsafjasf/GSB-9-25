"""Backend selector library (stdlib only).

Provides two load-balancing strategies behind a unified interface:

- SmoothWeightedRoundRobin: nginx-style smooth WRR. Deterministic, and
  produces exactly weight-proportional distribution over any multiple of
  sum(weights) selections.
- LeastConnections: picks the eligible node with the lowest
  active_connections / weight ratio.

Common semantics
----------------
- A node is *eligible* iff it is not drained and its weight > 0.
- Drained nodes are never selected. Restored nodes become eligible
  immediately; see each strategy's docstring for the re-participation bound.
- Weight-0 nodes never participate in selection.
- select() raises NoBackendAvailableError when no node is eligible.
- All selection results are deterministic: given the same sequence of
  operations (add/remove/weight/drain/restore/select/release), the selected
  node sequence is identical. Tie-breaking is always by insertion order
  (the order nodes were first added); earlier-inserted nodes win ties.
- All public methods are thread-safe (guarded by one lock).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional


class NoBackendAvailableError(Exception):
    """Raised by select() when every node is drained or has weight 0."""


@dataclass
class Node:
    """A backend node.

    Attributes:
        node_id: unique identifier.
        weight: static weight (>= 0). Weight 0 means never selected.
        active_connections: in-flight connections (maintained by the
            selector via select()/release()).
        drained: True while health-check has pulled the node out.
    """

    node_id: str
    weight: int = 1
    active_connections: int = 0
    drained: bool = False
    # internal smooth-WRR state
    _current_weight: int = 0

    @property
    def eligible(self) -> bool:
        return not self.drained and self.weight > 0


class BaseSelector:
    """Unified interface shared by all strategies."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # dict preserves insertion order -> deterministic tie-breaking
        self._nodes: Dict[str, Node] = {}
        self._select_counts: Dict[str, int] = {}

    # ----- membership / configuration -------------------------------------

    def add_node(self, node_id: str, weight: int = 1) -> None:
        if weight < 0:
            raise ValueError("weight must be >= 0")
        with self._lock:
            if node_id in self._nodes:
                raise ValueError(f"node {node_id!r} already exists")
            self._nodes[node_id] = Node(node_id=node_id, weight=weight)
            self._select_counts[node_id] = 0

    def remove_node(self, node_id: str) -> None:
        with self._lock:
            self._nodes.pop(node_id, None)
            self._select_counts.pop(node_id, None)

    def set_weight(self, node_id: str, weight: int) -> None:
        if weight < 0:
            raise ValueError("weight must be >= 0")
        with self._lock:
            self._nodes[node_id].weight = weight

    def drain(self, node_id: str) -> None:
        """Pull a node out of rotation (e.g. failed health check)."""
        with self._lock:
            self._nodes[node_id].drained = True

    def restore(self, node_id: str) -> None:
        """Return a drained node to rotation.

        Its in-flight connection counter is reset to 0: while drained the
        node received no new connections, so old ones are assumed finished.
        """
        with self._lock:
            node = self._nodes[node_id]
            node.drained = False
            node.active_connections = 0

    # ----- selection -------------------------------------------------------

    def select(self) -> str:
        """Pick a node and record one in-flight connection on it.

        Returns the node_id. Raises NoBackendAvailableError if no node is
        eligible. Must be paired with release() when the request finishes.
        """
        with self._lock:
            node = self._pick_locked()
            node.active_connections += 1
            self._select_counts[node.node_id] += 1
            return node.node_id

    def release(self, node_id: str) -> None:
        """Mark one in-flight connection on node_id as finished."""
        with self._lock:
            node = self._nodes[node_id]
            if node.active_connections > 0:
                node.active_connections -= 1

    def _pick_locked(self) -> Node:
        raise NotImplementedError

    def _eligible_locked(self) -> List[Node]:
        return [n for n in self._nodes.values() if n.eligible]

    # ----- introspection ---------------------------------------------------

    def stats(self) -> Dict[str, dict]:
        """Per-node selection counts, shares, and current state."""
        with self._lock:
            total = sum(self._select_counts.values())
            return {
                nid: {
                    "count": self._select_counts.get(nid, 0),
                    "share": (self._select_counts.get(nid, 0) / total)
                    if total
                    else 0.0,
                    "weight": node.weight,
                    "drained": node.drained,
                    "active_connections": node.active_connections,
                }
                for nid, node in self._nodes.items()
            }

    def total_selected(self) -> int:
        with self._lock:
            return sum(self._select_counts.values())


class SmoothWeightedRoundRobin(BaseSelector):
    """Nginx-style smooth weighted round robin.

    Each selection: every eligible node's current_weight += weight; the
    node with the highest current_weight wins (ties -> earliest inserted);
    the winner's current_weight -= sum(eligible weights).

    Re-participation bound: a restored node with weight w among eligible
    total weight T is selected at least once within the next
    ceil(T / w) select() calls (its current_weight grows by w per call and
    starts no lower than -T, so it must win within that window).
    """

    def _pick_locked(self) -> Node:
        eligible = self._eligible_locked()
        if not eligible:
            raise NoBackendAvailableError("no eligible backend node")
        total = sum(n.weight for n in eligible)
        best: Optional[Node] = None
        for node in eligible:
            node._current_weight += node.weight
            # strict '>' keeps the earliest-inserted node on ties
            if best is None or node._current_weight > best._current_weight:
                best = node
        assert best is not None
        best._current_weight -= total
        return best


class LeastConnections(BaseSelector):
    """Weighted least connections.

    Score = active_connections / weight; the eligible node with the lowest
    score wins (ties -> earliest inserted). Dividing by weight lets a
    higher-capacity node absorb proportionally more in-flight connections.

    Re-participation bound: a restored node starts with 0 active
    connections, hence score 0, which is the minimum possible; it is
    therefore selected within (number of eligible nodes) select() calls at
    most, and immediately if every other node has >= 1 in-flight connection.
    """

    def _pick_locked(self) -> Node:
        eligible = self._eligible_locked()
        if not eligible:
            raise NoBackendAvailableError("no eligible backend node")
        best: Optional[Node] = None
        best_score = 0.0
        for node in eligible:
            score = node.active_connections / node.weight
            if best is None or score < best_score:
                best = node
                best_score = score
        assert best is not None
        return best
