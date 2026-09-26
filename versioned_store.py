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
            _diff(da.get(key), db.get(key), path + (key,),
                  added, removed, modified)
    elif a_map:
        _collect_leaves(a, path, removed)
    elif b_map:
        _collect_leaves(b, path, added)
    elif a is None and b is not None:
        added.append(path)
    elif a is not None and b is None:
        removed.append(path)
    elif a != b:
        modified.append(path)


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
    def snapshot(self):
        """Commit the working state as a new version. O(1). Returns its id."""
        vid = self._next_id
        self._next_id += 1
        self._versions[vid] = self._root
        return vid

    def checkout(self, version):
        """Roll the working state back (or forward) to `version`."""
        if version not in self._versions:
            raise KeyError(f"unknown version: {version}")
        self._root = self._versions[version]

    def delete_version(self, version):
        """Delete a snapshot. Nodes not shared with other versions are freed."""
        if version not in self._versions:
            raise KeyError(f"unknown version: {version}")
        del self._versions[version]

    def versions(self):
        """Ids of all live snapshots."""
        return sorted(self._versions)

    # ------------------------------------------------------------------ diff
    def diff(self, version_a, version_b):
        """Difference between two versions.

        Returns {"added": [...], "removed": [...], "modified": [...]}, each
        a sorted list of leaf paths. Shared subtrees are skipped via identity
        comparison, so unchanged parts of the tree cost nothing.
        """
        added, removed, modified = [], [], []
        _diff(self._versions[version_a], self._versions[version_b], (),
              added, removed, modified)
        return {
            "added": sorted(added),
            "removed": sorted(removed),
            "modified": sorted(modified),
        }
