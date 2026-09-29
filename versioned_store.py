"""Versioned snapshot store with structural sharing.

A persistent (immutable) nested-map store. Each map level is a HAMT
(hash array mapped trie, 32-way branching), so:

- snapshot() is O(1): it only pins the current root node;
- a write copies only the nodes along one trie path per nesting level,
  i.e. O(path_depth x log32(map_size)) small nodes -- proportional to the
  *change*, independent of the total data size;
- unchanged subtrees are shared between versions and are never mutated in
  place, giving strong isolation between snapshots;
- deleting a version drops its root reference; nodes not shared with other
  versions are reclaimed by reference counting.

Only the Python standard library is used.
"""

from __future__ import annotations

import copy
import time

_BITS = 5                     # 32-way branching
_MASK = (1 << _BITS) - 1
_HASH_MASK = (1 << 64) - 1    # normalize Py_hash_t to unsigned 64-bit
_MISSING = object()


class _Counted:
    """Base class that tracks how many HAMT nodes are currently alive."""

    __slots__ = ()
    live_count = 0

    def __init__(self):
        _Counted.live_count += 1

    def __del__(self):
        _Counted.live_count -= 1


class _Node(_Counted):
    """Bitmap-indexed internal HAMT node."""

    __slots__ = ("bitmap", "items")

    def __init__(self, bitmap, items):
        super().__init__()
        self.bitmap = bitmap
        self.items = items  # list of _Node | _Leaf | _Collision


class _Leaf(_Counted):
    """Single key/value entry. Also the root of a one-entry map."""

    __slots__ = ("hash", "key", "value")

    def __init__(self, h, key, value):
        super().__init__()
        self.hash = h
        self.key = key
        self.value = value


class _Collision(_Counted):
    """Several leaves sharing one full 64-bit hash."""

    __slots__ = ("hash", "leaves")

    def __init__(self, h, leaves):
        super().__init__()
        self.hash = h
        self.leaves = leaves


def live_node_count():
    """Number of HAMT nodes currently alive (shared + exclusive)."""
    return _Counted.live_count


def _norm_hash(key):
    return hash(key) & _HASH_MASK


def _is_map(value):
    return isinstance(value, (_Node, _Leaf, _Collision))


# --------------------------------------------------------------------- HAMT

def _merge(shift, a, b):
    """Combine two terminals with different hashes into a common subtree."""
    fa = (a.hash >> shift) & _MASK
    fb = (b.hash >> shift) & _MASK
    if fa == fb:
        return _Node(1 << fa, [_merge(shift + _BITS, a, b)])
    if fa < fb:
        return _Node((1 << fa) | (1 << fb), [a, b])
    return _Node((1 << fa) | (1 << fb), [b, a])


def _hamt_assoc(node, shift, h, key, value):
    """Return a new HAMT with key->value; the input tree is untouched."""
    if node is None:
        return _Leaf(h, key, value)
    if isinstance(node, _Node):
        frag = (h >> shift) & _MASK
        bit = 1 << frag
        idx = (node.bitmap & (bit - 1)).bit_count()
        if node.bitmap & bit:
            child = _hamt_assoc(node.items[idx], shift + _BITS, h, key, value)
            if child is node.items[idx]:
                return node
            items = list(node.items)
            items[idx] = child
            return _Node(node.bitmap, items)
        items = node.items[:idx] + [_Leaf(h, key, value)] + node.items[idx:]
        return _Node(node.bitmap | bit, items)
    if isinstance(node, _Leaf):
        if node.hash == h and node.key == key:
            return node if node.value is value else _Leaf(h, key, value)
        return _merge(shift, node, _Leaf(h, key, value))
    # _Collision
    if node.hash == h:
        for i, leaf in enumerate(node.leaves):
            if leaf.key == key:
                if leaf.value is value:
                    return node
                leaves = list(node.leaves)
                leaves[i] = _Leaf(h, key, value)
                return _Collision(h, leaves)
        return _Collision(h, node.leaves + [_Leaf(h, key, value)])
    return _merge(shift, node, _Leaf(h, key, value))


def _hamt_dissoc(node, shift, h, key):
    """Return a new HAMT without key (None if the map became empty)."""
    if node is None:
        return None
    if isinstance(node, _Node):
        frag = (h >> shift) & _MASK
        bit = 1 << frag
        if not (node.bitmap & bit):
            return node
        idx = (node.bitmap & (bit - 1)).bit_count()
        child = _hamt_dissoc(node.items[idx], shift + _BITS, h, key)
        if child is node.items[idx]:
            return node
        if child is None:
            items = node.items[:idx] + node.items[idx + 1:]
            if not items:
                return None
            if len(items) == 1 and not isinstance(items[0], _Node):
                return items[0]  # collapse to a bare terminal
            return _Node(node.bitmap & ~bit, items)
        items = list(node.items)
        items[idx] = child
        return _Node(node.bitmap, items)
    if isinstance(node, _Leaf):
        return None if (node.hash == h and node.key == key) else node
    # _Collision
    if node.hash == h:
        for i, leaf in enumerate(node.leaves):
            if leaf.key == key:
                leaves = node.leaves[:i] + node.leaves[i + 1:]
                return leaves[0] if len(leaves) == 1 else _Collision(h, leaves)
    return node


def _hamt_lookup(node, shift, h, key):
    while isinstance(node, _Node):
        frag = (h >> shift) & _MASK
        bit = 1 << frag
        if not (node.bitmap & bit):
            return _MISSING
        node = node.items[(node.bitmap & (bit - 1)).bit_count()]
        shift += _BITS
    if isinstance(node, _Leaf):
        return node.value if (node.hash == h and node.key == key) else _MISSING
    if isinstance(node, _Collision) and node.hash == h:
        for leaf in node.leaves:
            if leaf.key == key:
                return leaf.value
    return _MISSING


def _hamt_items(node):
    if isinstance(node, _Node):
        for item in node.items:
            yield from _hamt_items(item)
    elif isinstance(node, _Leaf):
        yield node.key, node.value
    elif isinstance(node, _Collision):
        for leaf in node.leaves:
            yield leaf.key, leaf.value


_EMPTY_MAP = _Node(0, [])


def _map_from_plain(value):
    """Build a persistent map from a plain (possibly nested) dict."""
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if not value:
        return _EMPTY_MAP
    root = None
    for key, child in value.items():
        root = _hamt_assoc(root, 0, _norm_hash(key), key, _map_from_plain(child))
    return root


# ------------------------------------------------------- nested-map helpers

def _put(root, path, value):
    key = path[0]
    h = _norm_hash(key)
    if len(path) == 1:
        return _hamt_assoc(root, 0, h, key, value)
    sub = _hamt_lookup(root, 0, h, key)
    if not _is_map(sub):
        sub = None  # overwrite a leaf with a new sub-map
    return _hamt_assoc(root, 0, h, key, _put(sub, path[1:], value))


def _remove(root, path):
    if root is None:
        return None
    key = path[0]
    h = _norm_hash(key)
    if len(path) == 1:
        return _hamt_dissoc(root, 0, h, key)
    sub = _hamt_lookup(root, 0, h, key)
    if not _is_map(sub):
        return root  # path does not exist: share the tree as-is
    new_sub = _remove(sub, path[1:])
    if new_sub is sub:
        return root
    if new_sub is None:
        return _hamt_dissoc(root, 0, h, key)
    return _hamt_assoc(root, 0, h, key, new_sub)


def _get(root, path):
    node = root
    for key in path:
        if not _is_map(node):
            return _MISSING
        node = _hamt_lookup(node, 0, _norm_hash(key), key)
        if node is _MISSING:
            return _MISSING
    return node


def _materialize(node):
    if _is_map(node):
        return {k: _materialize(v) for k, v in _hamt_items(node)}
    return copy.deepcopy(node)


def _collect_leaves(node, path, out):
    if _is_map(node):
        for key, child in _hamt_items(node):
            _collect_leaves(child, path + (key,), out)
    else:
        out.append(path)


def _diff(a, b, path, added, removed, modified):
    if a is b:
        return  # shared subtree: identical by construction
    a_map = _is_map(a)
    b_map = _is_map(b)
    if a_map and b_map:
        da = dict(_hamt_items(a))
        db = dict(_hamt_items(b))
        for key in da.keys() | db.keys():
            _diff(da.get(key, _MISSING), db.get(key, _MISSING), path + (key,),
                  added, removed, modified)
        return
    if a is _MISSING and b is _MISSING:
        return
    if a_map:
        _collect_leaves(a, path, removed)
        if b is not _MISSING:
            added.append(path)  # leaf took the subtree's place
        return
    if b_map:
        if a is not _MISSING:
            removed.append(path)  # leaf was replaced by a subtree
        _collect_leaves(b, path, added)
        return
    if a is _MISSING:
        added.append(path)
    elif b is _MISSING:
        removed.append(path)
    elif a != b:
        modified.append(path)


def _leaf_entries(node, path, op, out):
    """Append one field-level entry per leaf under `node`."""
    if _is_map(node):
        for key, child in _hamt_items(node):
            _leaf_entries(child, path + (key,), op, out)
    elif op == "added":
        out.append({"path": path, "op": op, "new": copy.deepcopy(node)})
    else:
        out.append({"path": path, "op": op, "old": copy.deepcopy(node)})


def _diff_entries(a, b, path, out):
    """Field-level diff: one {"path", "op", ...} entry per differing leaf."""
    if a is b:
        return  # shared subtree: identical by construction
    a_map = _is_map(a)
    b_map = _is_map(b)
    if a_map and b_map:
        da = dict(_hamt_items(a))
        db = dict(_hamt_items(b))
        for key in da.keys() | db.keys():
            _diff_entries(da.get(key, _MISSING), db.get(key, _MISSING),
                          path + (key,), out)
        return
    if a is _MISSING and b is _MISSING:
        return
    if a_map:
        _leaf_entries(a, path, "removed", out)
        if b is not _MISSING:  # leaf took the subtree's place
            out.append({"path": path, "op": "added", "new": copy.deepcopy(b)})
        return
    if b_map:
        if a is not _MISSING:  # leaf was replaced by a subtree
            out.append({"path": path, "op": "removed",
                        "old": copy.deepcopy(a)})
        _leaf_entries(b, path, "added", out)
        return
    if a is _MISSING:
        out.append({"path": path, "op": "added", "new": copy.deepcopy(b)})
    elif b is _MISSING:
        out.append({"path": path, "op": "removed", "old": copy.deepcopy(a)})
    elif a != b:
        out.append({"path": path, "op": "modified",
                    "old": copy.deepcopy(a), "new": copy.deepcopy(b)})


def _root_or_missing(root):
    return _MISSING if root is None else root


def _reachable_nodes(roots):
    """Ids of all HAMT nodes reachable from any of `roots`."""
    seen = set()
    stack = [r for r in roots if r is not None]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, _Node):
            stack.extend(node.items)
        elif isinstance(node, _Collision):
            stack.extend(node.leaves)
    return seen


# --------------------------------------------------------------------- API

class VersionedStore:
    """A versioned nested-map store with cheap snapshots and rollback.

    Keys are paths (tuples) into the nested structure, e.g.
    ``store.set(("users", "alice", "age"), 30)``. Dict values become
    sub-maps; any other value is stored as an opaque leaf (deep-copied on
    write and on read, so mutable leaves cannot leak between versions).
    """

    def __init__(self, initial=None):
        self._versions = {}   # version id -> root node
        self._meta = {}       # version id -> {"created_at": float, "pins": int}
        self._root = None     # current working root (None = empty store)
        self._next_id = 0
        if initial:
            self._root = _map_from_plain(initial)

    @classmethod
    def from_dict(cls, data):
        """Build a store from a nested plain dict in one bulk load."""
        return cls(data)

    # ---------------------------------------------------------------- writes
    def set(self, path, value):
        """Set `value` at `path` in the working version."""
        path = tuple(path)
        if not path:
            raise ValueError("path must be non-empty")
        self._root = _put(self._root, path, _map_from_plain(value))

    def delete(self, path):
        """Remove `path` from the working version (no-op if absent)."""
        path = tuple(path)
        if not path:
            raise ValueError("path must be non-empty")
        self._root = _remove(self._root, path)

    # ----------------------------------------------------------------- reads
    def get(self, path, version=None):
        """Read the value at `path` in `version` (default: working version)."""
        root = self._root if version is None else self._versions[version]
        result = _get(root, tuple(path))
        if result is _MISSING:
            raise KeyError(tuple(path))
        return _materialize(result)

    def to_dict(self, version=None):
        """Materialize a version (default: working version) as a plain dict."""
        root = self._root if version is None else self._versions[version]
        return _materialize(root) if root is not None else {}

    # -------------------------------------------------------------- versions
    def snapshot(self, at=None):
        """Commit the working state as a new version. O(1). Returns its id.

        `at` optionally overrides the creation timestamp (used by the
        retention policy); defaults to the current wall-clock time.
        """
        vid = self._next_id
        self._next_id += 1
        self._versions[vid] = self._root
        self._meta[vid] = {
            "created_at": time.time() if at is None else at,
            "pins": 0,
        }
        return vid

    def checkout(self, version):
        """Roll the working state back (or forward) to `version`."""
        if version not in self._versions:
            raise KeyError(f"unknown version: {version}")
        self._root = self._versions[version]

    def delete_version(self, version):
        """Delete a snapshot. Nodes not shared with other versions are freed.

        Pinned (still referenced) versions cannot be deleted.
        """
        if version not in self._versions:
            raise KeyError(f"unknown version: {version}")
        if self._meta[version]["pins"]:
            raise ValueError(
                f"version {version} is pinned "
                f"(refs={self._meta[version]['pins']}); unpin it first")
        del self._versions[version]
        del self._meta[version]

    def versions(self):
        """Ids of all live snapshots."""
        return sorted(self._versions)

    # ------------------------------------------------------------------ pins
    def pin(self, version):
        """Add an external reference to `version`: cleanup will keep it."""
        if version not in self._versions:
            raise KeyError(f"unknown version: {version}")
        self._meta[version]["pins"] += 1

    def unpin(self, version):
        """Drop one external reference previously added with `pin()`."""
        if version not in self._versions:
            raise KeyError(f"unknown version: {version}")
        if not self._meta[version]["pins"]:
            raise ValueError(f"version {version} is not pinned")
        self._meta[version]["pins"] -= 1

    def refs(self, version):
        """Number of external references (pins) held on `version`."""
        if version not in self._versions:
            raise KeyError(f"unknown version: {version}")
        return self._meta[version]["pins"]

    def version_info(self, version):
        """Creation timestamp and pin count for `version`."""
        if version not in self._versions:
            raise KeyError(f"unknown version: {version}")
        return dict(self._meta[version])

    # ------------------------------------------------------------------ diff
    def diff(self, version_a, version_b):
        """Difference between two versions.

        Returns {"added": [...], "removed": [...], "modified": [...]}, each
        a sorted list of leaf paths. Shared subtrees are skipped via identity
        comparison, so unchanged parts of the tree cost nothing.
        """
        added, removed, modified = [], [], []
        _diff(_root_or_missing(self._versions[version_a]),
              _root_or_missing(self._versions[version_b]), (),
              added, removed, modified)
        return {
            "added": sorted(added),
            "removed": sorted(removed),
            "modified": sorted(modified),
        }

    def diff_fields(self, version_a, version_b):
        """Field-by-field difference between two versions, with values.

        Returns a list of entries sorted by path, one per differing field:

            {"path": ("users", "alice", "age"), "op": "modified",
             "old": 30, "new": 31}

        "added" entries carry only "new", "removed" entries only "old".
        Each entry can be checked individually against `get(path, version)`.
        """
        entries = []
        _diff_entries(_root_or_missing(self._versions[version_a]),
                      _root_or_missing(self._versions[version_b]), (), entries)
        entries.sort(key=lambda e: e["path"])
        return entries

    # ---------------------------------------------------------------- replay
    def apply_entries(self, entries):
        """Replay field-level diff entries onto the working state.

        Entries are applied parents-before-children (sorted by path), so a
        removed leaf and a re-added subtree below it cannot interfere.
        Returns the number of entries applied.
        """
        for entry in sorted(entries, key=lambda e: e["path"]):
            if entry["op"] == "removed":
                self.delete(entry["path"])
            else:
                self.set(entry["path"], entry["new"])
        return len(entries)

    def rollback_to(self, version, verify=True):
        """Incrementally roll the working state back (or forward) to `version`.

        Instead of rebuilding the whole dataset, this computes the
        field-level diff between the working state and the target version
        and replays only those entries. With `verify` (default), the result
        is checked field-by-field against a full rebuild of the target
        version (`to_dict(version)`).

        Returns {"replayed": n, "nodes_allocated": m, "verified": bool}.
        """
        if version not in self._versions:
            raise KeyError(f"unknown version: {version}")
        entries = []
        _diff_entries(_root_or_missing(self._root),
                      _root_or_missing(self._versions[version]), (), entries)
        before = live_node_count()
        replayed = self.apply_entries(entries)
        result = {
            "replayed": replayed,
            "nodes_allocated": live_node_count() - before,
            "verified": False,
        }
        if verify:
            if self.to_dict() != self.to_dict(version):
                raise AssertionError(
                    f"incremental rollback to version {version} diverged "
                    "from a full rebuild")
            result["verified"] = True
        return result

    # -------------------------------------------------------------- retention
    def retention_plan(self, max_count=None, max_age=None, now=None):
        """Dry-run a retention policy; change nothing.

        A version is kept if ANY of these hold:
          - it is pinned (still referenced externally);
          - it is the latest version;
          - it is among the newest `max_count` versions (if given);
          - its age is below `max_age` seconds (if given).

        Returns a plan describing, per version, whether it would be kept
        (with reasons) or dropped, plus the exact impact scope: how many
        HAMT nodes would be freed and how many would remain.
        """
        if max_count is None and max_age is None:
            raise ValueError("provide max_count and/or max_age")
        now = time.time() if now is None else now
        vids = self.versions()
        latest = vids[-1] if vids else None
        keep, drop = [], []
        for rank, vid in enumerate(reversed(vids)):  # rank 0 == newest
            meta = self._meta[vid]
            age = now - meta["created_at"]
            reasons = []
            if meta["pins"]:
                reasons.append(f"pinned (refs={meta['pins']})")
            if vid == latest:
                reasons.append("latest version")
            if max_count is not None and rank < max_count:
                reasons.append(f"within newest {max_count}")
            if max_age is not None and age < max_age:
                reasons.append(f"age {age:.1f}s < max_age {max_age}s")
            record = {"version": vid, "created_at": meta["created_at"],
                      "age": age}
            if reasons:
                record["reasons"] = reasons
                keep.append(record)
            else:
                drop.append(record)
        keep.reverse()
        drop.reverse()

        kept_roots = [self._versions[e["version"]] for e in keep]
        kept_roots.append(self._root)  # working state must stay valid too
        drop_roots = [self._versions[e["version"]] for e in drop]
        freed = len(_reachable_nodes(drop_roots)
                    - _reachable_nodes(kept_roots))
        return {
            "policy": {"max_count": max_count, "max_age": max_age},
            "now": now,
            "keep": keep,
            "drop": drop,
            "impact": {
                "versions_kept": len(keep),
                "versions_dropped": len(drop),
                "nodes_freed": freed,
                "nodes_remaining": live_node_count() - freed,
            },
        }

    def apply_retention(self, max_count=None, max_age=None, now=None):
        """Run `retention_plan` and delete the versions it marked for drop.

        Pinned versions are never dropped, so cleanup cannot affect versions
        that are still referenced. Returns the plan, extended with the
        actually freed node count (exactly matches the dry-run estimate).
        """
        plan = self.retention_plan(max_count, max_age, now)
        before = live_node_count()
        for entry in plan["drop"]:
            vid = entry["version"]
            del self._versions[vid]
            del self._meta[vid]
        plan["impact"]["nodes_freed_actual"] = before - live_node_count()
        return plan
