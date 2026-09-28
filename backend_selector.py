"""Backend selector library (stdlib only).

Provides two load-balancing strategies behind a unified interface:

- SmoothWeightedRoundRobin: nginx-style smooth WRR. Deterministic, and
  produces exactly weight-proportional distribution over any multiple of
  sum(weights) selections.
- LeastConnections: picks the eligible node with the lowest
  active_connections / weight ratio.

Health-check lifecycle
----------------------
Every node is in exactly one of three states:

- HEALTHY: serves normal traffic.
- OPEN: pulled out of rotation after a failed health check (drain()). It
  cannot be selected and only receives probes after the cooldown.
- HALF_OPEN: the cooldown has elapsed; exactly one probe request may be
  in flight. A failed probe re-opens the node (cooldown restarts); after
  ``required_successes`` consecutive successful probes the node becomes
  HEALTHY again (fully recovered). A half-open node never serves normal
  traffic, so at most one exploratory request is exposed at a time.

Typical loop (all operations are optional/non-blocking)::

    sel.health_tick()          # OPEN nodes whose cooldown elapsed -> HALF_OPEN
    probe = sel.select_probe() # route one probe request to a HALF_OPEN node
    if probe is not None:
        ok = do_probe(probe)   # caller performs the actual health request
        sel.report_probe(probe, ok)

Common semantics
----------------
- A node participates in normal select() iff it is HEALTHY and its
  weight > 0. OPEN and HALF_OPEN nodes are never returned by select();
  weight-0 nodes never participate at all.
- select() raises NoBackendAvailableError when no HEALTHY, weight>0 node
  exists (a lone half-open probe does NOT silently take production
  traffic).
- All selection results are deterministic: given the same sequence of
  operations (add/remove/set_weight/drain/tick/select_probe/report_probe/
  select/release) on a clock that returns the same time values, the
  selected node sequence is identical. Tie-breaking is always by
  insertion order (the order nodes were first added); a just-recovered
  node wins ties against other zero-priority candidates.
- All public methods are thread-safe (guarded by one lock).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

# health states
HEALTHY = "HEALTHY"
OPEN = "OPEN"          # removed from rotation, cooling down
HALF_OPEN = "HALF_OPEN"  # probing with exploratory requests only


class NoBackendAvailableError(Exception):
    """Raised by select() when no healthy weight>0 node is available."""


class MonotonicClock:
    """Default time source. Pass any zero-arg callable returning a
    monotonically non-decreasing number to make runs scriptable."""

    def __call__(self) -> float:
        return time.monotonic()


@dataclass
class Node:
    """A backend node.

    Attributes:
        node_id: unique identifier.
        weight: static weight (>= 0). Weight 0 means never selected.
        active_connections: in-flight connections (maintained by the
            selector via select()/release()). Probes are not counted.
        state: HEALTHY / OPEN / HALF_OPEN.
        cooldown_until: earliest time (clock units) at which an OPEN node
            may transition to HALF_OPEN.
        probe_successes: consecutive successful probes in HALF_OPEN.
        probe_in_flight: a probe has been handed out and not yet
            reported; while True no further probe is handed to the node.
    """

    node_id: str
    weight: int = 1
    active_connections: int = 0
    state: str = HEALTHY
    cooldown_until: float = 0.0
    probe_successes: int = 0
    probe_in_flight: bool = False
    # internal smooth-WRR state
    _current_weight: int = 0
    # internal least-connections tie-break priority on recovery (0 = none)
    _recovery_rank: int = 0

    @property
    def eligible(self) -> bool:
        """Participates in normal select(): healthy and weight > 0."""
        return self.state == HEALTHY and self.weight > 0

    @property
    def drained(self) -> bool:
        """Backwards-compatible view: out of normal rotation."""
        return self.state != HEALTHY


class BaseSelector:
    """Unified interface shared by all strategies."""

    def __init__(
        self,
        cooldown: float = 5.0,
        required_successes: int = 2,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if cooldown < 0:
            raise ValueError("cooldown must be >= 0")
        if required_successes < 1:
            raise ValueError("required_successes must be >= 1")
        self._lock = threading.Lock()
        # dict preserves insertion order -> deterministic tie-breaking
        self._nodes: Dict[str, Node] = {}
        self._select_counts: Dict[str, int] = {}
        self._probe_counts: Dict[str, int] = {}
        # insertion position for deterministic tie-breaking
        self._insertion_index: Dict[str, int] = {}
        self._next_insertion_index = 0
        self._cooldown = float(cooldown)
        self._required_successes = required_successes
        self._clock = clock or MonotonicClock()
        # monotonic priority: a just-recovered node wins selection ties
        self._recovery_epoch: int = 0

    # ----- membership / configuration -------------------------------------

    def add_node(self, node_id: str, weight: int = 1) -> None:
        if weight < 0:
            raise ValueError("weight must be >= 0")
        with self._lock:
            if node_id in self._nodes:
                raise ValueError(f"node {node_id!r} already exists")
            self._nodes[node_id] = Node(node_id=node_id, weight=weight)
            self._select_counts[node_id] = 0
            self._probe_counts[node_id] = 0
            self._insertion_index[node_id] = self._next_insertion_index
            self._next_insertion_index += 1

    def remove_node(self, node_id: str) -> None:
        with self._lock:
            self._nodes.pop(node_id, None)
            self._select_counts.pop(node_id, None)
            self._probe_counts.pop(node_id, None)
            self._insertion_index.pop(node_id, None)

    def set_weight(self, node_id: str, weight: int) -> None:
        """Change a node's weight at runtime.

        Takes effect on the very next select(). Weight 0 withdraws the
        node immediately (it stays in whatever health state it was in and
        rejoins as soon as weight is raised again, subject to state).
        """
        if weight < 0:
            raise ValueError("weight must be >= 0")
        with self._lock:
            self._nodes[node_id].weight = weight

    # ----- health-check lifecycle -----------------------------------------

    def drain(self, node_id: str) -> None:
        """Pull a node out of rotation after a failed health check.

        The node enters OPEN for ``cooldown`` clock units. Calling drain()
        again restarts the cooldown and discards half-open probe progress.
        """
        with self._lock:
            node = self._nodes[node_id]
            node.state = OPEN
            node.cooldown_until = self._clock() + self._cooldown
            node.probe_successes = 0
            node.probe_in_flight = False

    def restore(self, node_id: str) -> None:
        """Force a node straight back to HEALTHY (manual/override path).

        The normal automated path is drain() -> cooldown -> HALF_OPEN
        probes -> HEALTHY. Its in-flight connection counter is reset to 0
        and strategy state is reset via _on_recover_locked() so the
        re-participation bound applies cleanly.
        """
        with self._lock:
            node = self._nodes[node_id]
            self._recover_locked(node)

    def health_tick(self) -> List[str]:
        """Advance the health state machine using the injected clock.

        Every OPEN node whose cooldown has elapsed becomes HALF_OPEN and
        becomes eligible for exactly one probe. Returns the list of node
        ids that transitioned OPEN -> HALF_OPEN during this call.
        """
        with self._lock:
            now = self._clock()
            transitioned: List[str] = []
            for node in self._nodes.values():
                if (
                    node.state == OPEN
                    and not node.probe_in_flight
                    and now >= node.cooldown_until
                ):
                    node.state = HALF_OPEN
                    node.probe_successes = 0
                    transitioned.append(node.node_id)
            return transitioned

    def select_probe(self) -> Optional[str]:
        """Hand out at most one exploratory probe to a HALF_OPEN node.

        Probe nodes never appear in normal select(): callers must route
        the returned id with a real health-check request and then call
        report_probe(). Returns None when no node currently needs a
        probe. Ties (multiple cooldown-expired nodes) break by insertion
        order, so results are reproducible.
        """
        with self._lock:
            return self._select_probe_locked(set())

    def _select_probe_locked(self, exclude: set) -> Optional[str]:
        now = self._clock()
        for node in self._nodes.values():
            if node.state == OPEN and now >= node.cooldown_until:
                node.state = HALF_OPEN
                node.probe_successes = 0
            if (
                node.state == HALF_OPEN
                and not node.probe_in_flight
                and node.weight > 0
                and node.node_id not in exclude
            ):
                node.probe_in_flight = True
                self._probe_counts[node.node_id] += 1
                return node.node_id
        return None

    def report_probe(self, node_id: str, ok: bool) -> str:
        """Report the result of a probe handed out by select_probe().

        - failure: HALF_OPEN -> OPEN and the cooldown restarts.
        - success: one consecutive success is recorded; after
          ``required_successes`` consecutive successes the node becomes
          HEALTHY (fully recovered).
        Stale reports (node re-drained, no outstanding probe) are
        ignored. Returns the node's resulting state.
        """
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None or not node.probe_in_flight:
                return node.state if node is not None else OPEN
            node.probe_in_flight = False
            if node.state != HALF_OPEN:
                return node.state
            if not ok:
                node.state = OPEN
                node.probe_successes = 0
                node.cooldown_until = self._clock() + self._cooldown
                return OPEN
            node.probe_successes += 1
            if node.probe_successes >= self._required_successes:
                self._recover_locked(node)
            return node.state

    def run_health_checks(self, probe_fn: Callable[[str], bool]) -> Dict[str, str]:
        """One full health-check pass: tick, probe due nodes, report.

        ``probe_fn`` is called once per due node with the node id and must
        return True on success. Long-running schedulers call this on a
        timer. Returns {node_id: resulting state} for probed nodes.
        """
        results: Dict[str, str] = {}
        self.health_tick()
        probed: set = set()  # at most one probe per node per pass
        while True:
            with self._lock:
                node_id = self._select_probe_locked(probed)
            if node_id is None:
                return results
            probed.add(node_id)
            results[node_id] = self.report_probe(node_id, probe_fn(node_id))

    def _recover_locked(self, node: Node) -> None:
        node.state = HEALTHY
        node.cooldown_until = 0.0
        node.probe_successes = 0
        node.probe_in_flight = False
        node.active_connections = 0
        self._recovery_epoch += 1
        node._recovery_rank = self._recovery_epoch  # type: ignore[attr-defined]
        self._on_recover_locked(node)

    def _on_recover_locked(self, node: Node) -> None:
        """Strategy-specific state reset on full recovery."""

    # ----- selection -------------------------------------------------------

    def select(self) -> str:
        """Pick a HEALTHY weight>0 node and record one in-flight connection.

        OPEN/HALF_OPEN nodes and weight-0 nodes are never returned.
        Raises NoBackendAvailableError if no node is eligible. Must be
        paired with release() when the request finishes.
        """
        with self._lock:
            node = self._pick_locked()
            node.active_connections += 1
            self._select_counts[node.node_id] += 1
            return node.node_id

    def release(self, node_id: str) -> None:
        """Mark one in-flight connection on node_id as finished."""
        with self._lock:
            node = self._nodes.get(node_id)
            if node is not None and node.active_connections > 0:
                node.active_connections -= 1

    def _pick_locked(self) -> Node:
        raise NotImplementedError

    def _eligible_locked(self) -> List[Node]:
        return [n for n in self._nodes.values() if n.eligible]

    # ----- introspection ---------------------------------------------------

    def state_of(self, node_id: str) -> str:
        with self._lock:
            return self._nodes[node_id].state

    def recovery_bound(self, node_id: str) -> Optional[int]:
        """Upper bound on select() calls until ``node_id`` is picked after
        it fully recovers (measured from the recovery instant, weights held
        fixed). None means the node can never be picked (weight 0) or is
        unknown. Strategy-specific, see each class docstring."""
        with self._lock:
            return self._recovery_bound_locked(node_id)

    def _recovery_bound_locked(self, node_id: str) -> Optional[int]:
        raise NotImplementedError

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
                    "state": node.state,
                    # backwards-compatible boolean alias
                    "drained": node.state != HEALTHY,
                    "probe_successes": node.probe_successes,
                    "probe_in_flight": node.probe_in_flight,
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

    On full recovery the recovered node's current_weight is reset to 0.
    """

    def _on_recover_locked(self, node: Node) -> None:
        node._current_weight = 0

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

    def _recovery_bound_locked(self, node_id: str) -> Optional[int]:
        """Worst case (weights held fixed from the recovery instant): the
        recovered node with weight w, eligible total T (including it), is
        picked within ceil(T / w) select() calls.

        Justification: when it recovers its current_weight is 0 while the
        other eligible nodes' current weights are all <= 0 and sum to 0;
        per call its current_weight gains w, so within ceil(T / w) calls
        it must exceed the others. Runtime weight changes during that
        window make the actual delay only shorter-or-equal; the method
        reports the bound implied by the weights visible *now*.
        """
        node = self._nodes.get(node_id)
        if node is None or node.weight <= 0:
            return None
        total = sum(
            n.weight for n in self._nodes.values() if n.weight > 0
        )
        return -(-total // node.weight)  # ceil(total / w)


class LeastConnections(BaseSelector):
    """Weighted least connections.

    Score = active_connections / weight; the eligible node with the lowest
    score wins. Dividing by weight lets a higher-capacity node absorb
    proportionally more in-flight connections.

    A fully recovered node has 0 in-flight connections (score 0, the
    global minimum) and additionally wins score ties via its recovery
    epoch; hence it is picked on the very next select() regardless of
    releases on other nodes. Tie-breaking among non-recovered zero-score
    nodes remains insertion order.
    """

    def _rank_locked(self, node: Node) -> Tuple[float, int, int]:
        # Lower is better: score, then not-recovered (epoch 0 sorts before
        # real epochs, so a recovered node must win, not lose), then
        # insertion position.
        recovered = 0 if getattr(node, "_recovery_rank", 0) else 1
        return (
            node.active_connections / node.weight,
            recovered,
            self._insertion_index[node.node_id],
        )

    def _pick_locked(self) -> Node:
        eligible = self._eligible_locked()
        if not eligible:
            raise NoBackendAvailableError("no eligible backend node")
        best: Optional[Node] = None
        best_rank: Optional[Tuple[float, int, int]] = None
        for node in eligible:
            rank = self._rank_locked(node)
            if best_rank is None or rank < best_rank:
                best, best_rank = node, rank
        assert best is not None
        # recovery priority is single-shot: afterwards it competes
        # normally by score, with insertion-order ties.
        best._recovery_rank = 0  # type: ignore[attr-defined]
        return best

    def _recovery_bound_locked(self, node_id: str) -> Optional[int]:
        node = self._nodes.get(node_id)
        if node is None or node.weight <= 0:
            return None
        return 1
