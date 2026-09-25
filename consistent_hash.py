"""Consistent hashing selector (stdlib only).

Design notes
------------
* Hash ring: sorted tuple of 64-bit hash points, each mapped to a node name.
  A key is assigned to the first point clockwise from hash(key).
* Virtual nodes: each physical node owns `round(base_vnodes * weight)` points
  on the ring, which smooths the key distribution and encodes weight.
* Order independence: the ring is rebuilt from ``sorted(nodes)`` and vnode
  points are derived from ``hash("{name}#{i}")`` only, so the resulting
  key->node mapping never depends on the order nodes were added in.
* Weight change: vnode points for index i are stable (``name#i``).  Raising a
  weight only *appends* new points, lowering it only *removes* trailing
  points.  Existing points keep their positions, so only the arcs claimed /
  released by the changed node migrate.
* Concurrency: the ring is an immutable ``(hashes, names)`` tuple pair.
  Writers build a new ring off-lock and swap the reference under a lock;
  readers grab the reference once and search without any locking, so
  concurrent readers never interfere with each other or with writers.
"""

from __future__ import annotations

import bisect
import hashlib
import threading
from typing import Dict, Hashable, List, Optional, Tuple

_RING_MOD = 1 << 64


def _hash64(text: str) -> int:
    """Stable 64-bit hash (md5 based; independent of PYTHONHASHSEED)."""
    return int.from_bytes(hashlib.md5(text.encode("utf-8")).digest()[:8], "big")


class ConsistentHash:
    """Weighted consistent-hash node selector, safe for concurrent reads."""

    def __init__(self, base_vnodes: int = 160) -> None:
        if base_vnodes < 1:
            raise ValueError("base_vnodes must be >= 1")
        self._base_vnodes = base_vnodes
        self._weights: Dict[str, float] = {}
        self._write_lock = threading.Lock()
        # Immutable snapshot: (sorted point hashes, parallel node names).
        self._ring: Tuple[Tuple[int, ...], Tuple[str, ...]] = ((), ())

    # ------------------------------------------------------------------ #
    # ring construction
    # ------------------------------------------------------------------ #
    def _vnode_count(self, weight: float) -> int:
        return max(1, round(self._base_vnodes * weight))

    def _build_ring(
        self, weights: Dict[str, float]
    ) -> Tuple[Tuple[int, ...], Tuple[str, ...]]:
        points: List[Tuple[int, str]] = []
        for name in sorted(weights):  # order-independent construction
            for i in range(self._vnode_count(weights[name])):
                points.append((_hash64(f"{name}#{i}"), name))
        points.sort()
        return (
            tuple(p for p, _ in points),
            tuple(n for _, n in points),
        )

    def _rebuild_locked(self) -> None:
        self._ring = self._build_ring(self._weights)

    # ------------------------------------------------------------------ #
    # mutations (serialized; readers are never blocked)
    # ------------------------------------------------------------------ #
    def add_node(self, name: str, weight: float = 1.0) -> None:
        if weight <= 0:
            raise ValueError("weight must be > 0")
        with self._write_lock:
            self._weights[name] = float(weight)
            self._rebuild_locked()

    def remove_node(self, name: str) -> None:
        with self._write_lock:
            if name not in self._weights:
                raise KeyError(name)
            del self._weights[name]
            self._rebuild_locked()

    def set_weight(self, name: str, weight: float) -> None:
        """Change a node's weight; vnode points for surviving indexes keep
        their positions, so migration stays proportional to the change."""
        if weight <= 0:
            raise ValueError("weight must be > 0")
        with self._write_lock:
            if name not in self._weights:
                raise KeyError(name)
            self._weights[name] = float(weight)
            self._rebuild_locked()

    # ------------------------------------------------------------------ #
    # read path (lock-free, safe to call from many threads at once)
    # ------------------------------------------------------------------ #
    def get_node(self, key: Hashable) -> Optional[str]:
        hashes, names = self._ring  # single atomic snapshot read
        if not hashes:
            return None
        point = _hash64(str(key))
        idx = bisect.bisect_left(hashes, point)
        if idx == len(hashes):  # wrap around the ring
            idx = 0
        return names[idx]

    def get_nodes(self, key: Hashable, count: int) -> List[str]:
        """First `count` distinct nodes clockwise from the key (for replicas)."""
        hashes, names = self._ring
        if not hashes or count <= 0:
            return []
        point = _hash64(str(key))
        idx = bisect.bisect_left(hashes, point)
        if idx == len(hashes):
            idx = 0
        result: List[str] = []
        seen = set()
        for offset in range(len(hashes)):
            name = names[(idx + offset) % len(hashes)]
            if name not in seen:
                seen.add(name)
                result.append(name)
                if len(result) == count:
                    break
        return result

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #
    @property
    def nodes(self) -> List[str]:
        return sorted(self._weights)

    @property
    def weights(self) -> Dict[str, float]:
        return dict(self._weights)

    @property
    def ring_size(self) -> int:
        return len(self._ring[0])

    def __len__(self) -> int:
        return len(self._weights)

    def __contains__(self, name: str) -> bool:
        return name in self._weights
