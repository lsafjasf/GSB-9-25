"""Chunked in-memory sorted index with snapshot-isolated reads.

Storage layout:
    The index is an ordered list of blocks. Each block owns two
    preallocated slot arrays (keys / values) of a fixed capacity and a
    ``count`` of live entries. Blocks hold disjoint, sorted key ranges.

Concurrency model:
    Blocks are *immutable once published*. Every mutation builds new
    block objects for the affected ranges and then publishes a single
    ``_Version`` object (block list + block starts + size) with one
    atomic reference swap (under a write lock). Readers load that one
    reference exactly once per operation, so they always see a
    self-consistent view: either the complete state before a delete or
    the complete state after it, never an intermediate mix.

Space reclamation:
    Point deletes keep the block's slot arrays (free slots become
    reusable capacity, visible as fragmentation). Range deletes drop
    fully-covered blocks wholesale and rebuild partially-covered edge
    blocks with exact-fit arrays, so the capacity of deleted interior
    keys is returned to the allocator immediately.
"""

import bisect
import threading

DEFAULT_BLOCK_CAPACITY = 1024


class _Block:
    """A fixed-capacity sorted run of key/value pairs.

    Never mutated after being published into an index's block list.
    """

    __slots__ = ("keys", "vals", "count")

    def __init__(self, capacity):
        self.keys = [None] * capacity
        self.vals = [None] * capacity
        self.count = 0

    @property
    def capacity(self):
        return len(self.keys)

    def pairs(self):
        return zip(self.keys[: self.count], self.vals[: self.count])


def _block_from_pairs(pairs, capacity=None):
    """Build a published-ready block from an iterable of (key, value)."""
    pairs = list(pairs)
    cap = max(len(pairs), capacity if capacity is not None else len(pairs), 1)
    block = _Block(cap)
    for i, (k, v) in enumerate(pairs):
        block.keys[i] = k
        block.vals[i] = v
    block.count = len(pairs)
    return block


def _clone_with_insert(block, pos, key, value):
    """Copy ``block`` with (key, value) inserted at ``pos`` (room required)."""
    nb = _Block(block.capacity)
    nb.keys[:pos] = block.keys[:pos]
    nb.keys[pos] = key
    nb.keys[pos + 1 : block.count + 1] = block.keys[pos : block.count]
    nb.vals[:pos] = block.vals[:pos]
    nb.vals[pos] = value
    nb.vals[pos + 1 : block.count + 1] = block.vals[pos : block.count]
    nb.count = block.count + 1
    return nb


def _clone_without(block, pos):
    """Copy ``block`` with the entry at ``pos`` removed (capacity kept)."""
    nb = _Block(block.capacity)
    nb.keys[:pos] = block.keys[:pos]
    nb.keys[pos : block.count - 1] = block.keys[pos + 1 : block.count]
    nb.vals[:pos] = block.vals[:pos]
    nb.vals[pos : block.count - 1] = block.vals[pos + 1 : block.count]
    nb.count = block.count - 1
    return nb


def _clone_with_value(block, pos, value):
    nb = _Block(block.capacity)
    nb.keys[: block.count] = block.keys[: block.count]
    nb.vals[: block.count] = block.vals[: block.count]
    nb.vals[pos] = value
    nb.count = block.count
    return nb


class _Version:
    """One atomically published state of the index.

    The block list, the per-block start keys, and the logical size are
    always replaced together as a single object, so a reader that loads
    the version reference once can never observe a mix of two states
    (e.g. new blocks paired with a stale size or stale starts).
    """

    __slots__ = ("blocks", "starts", "size")

    def __init__(self, blocks, size):
        self.blocks = blocks
        self.starts = [b.keys[0] for b in blocks]
        self.size = size


class Snapshot:
    """Immutable view of an index at a point in time."""

    __slots__ = ("_blocks", "_size")

    def __init__(self, blocks, size):
        self._blocks = blocks
        self._size = size

    def __len__(self):
        return self._size

    def items(self):
        for block in self._blocks:
            yield from block.pairs()

    def keys(self):
        for block in self._blocks:
            yield from block.keys[: block.count]

    def stats(self):
        capacity = sum(b.capacity for b in self._blocks)
        return {
            "size": self._size,
            "capacity": capacity,
            "blocks": len(self._blocks),
            "fragmentation": capacity - self._size,
        }


class SortedIndex:
    """Sorted unique-key index supporting point and range deletes."""

    def __init__(self, pairs=(), block_capacity=DEFAULT_BLOCK_CAPACITY):
        if block_capacity < 2:
            raise ValueError("block_capacity must be >= 2")
        self._block_capacity = block_capacity
        self._lock = threading.Lock()
        self._version = _Version([], 0)
        for k, v in pairs:
            self.put(k, v)

    @classmethod
    def bulk_load(cls, sorted_pairs, block_capacity=DEFAULT_BLOCK_CAPACITY):
        """Build an index from already-sorted unique pairs in O(n)."""
        idx = cls(block_capacity=block_capacity)
        pairs = list(sorted_pairs)
        blocks = []
        for i in range(0, len(pairs), idx._block_capacity):
            blocks.append(_block_from_pairs(pairs[i : i + idx._block_capacity],
                                            idx._block_capacity))
        idx._version = _Version(blocks, len(pairs))
        return idx

    # ---------------------------------------------------------- reads
    def snapshot(self):
        """Return an immutable, consistent view of the current state."""
        version = self._version
        return Snapshot(version.blocks, version.size)

    def __len__(self):
        return self._version.size

    def __contains__(self, key):
        version = self._version
        found, _, _, _ = self._locate_in(version.starts, version.blocks, key)
        return found

    def get(self, key, default=None):
        version = self._version
        found, block, pos, _ = self._locate_in(version.starts, version.blocks, key)
        if not found:
            return default
        return block.vals[pos]

    def items(self):
        """Iterate (key, value) in sorted order over a consistent snapshot."""
        return self.snapshot().items()

    def stats(self):
        return self.snapshot().stats()

    @staticmethod
    def _locate_in(starts, blocks, key):
        i = bisect.bisect_right(starts, key) - 1
        if i < 0:
            if not blocks:
                return False, None, 0, 0
            block = blocks[0]
            return False, block, 0, 0
        block = blocks[i]
        pos = bisect.bisect_left(block.keys, key, 0, block.count)
        found = pos < block.count and block.keys[pos] == key
        return found, block, pos, i

    # ---------------------------------------------------------- writes
    def _publish(self, blocks, size):
        # Single reference swap: blocks, starts and size become visible
        # to readers together, never piecemeal.
        self._version = _Version(blocks, size)

    def put(self, key, value):
        with self._lock:
            version = self._version
            blocks, starts = version.blocks, version.starts
            if not blocks:
                nb = _Block(self._block_capacity)
                nb.keys[0] = key
                nb.vals[0] = value
                nb.count = 1
                self._publish([nb], 1)
                return
            found, block, pos, i = self._locate_in(starts, blocks, key)
            new_blocks = list(blocks)
            if found:
                new_blocks[i] = _clone_with_value(block, pos, value)
                self._publish(new_blocks, version.size)
                return
            if block.count < block.capacity:
                new_blocks[i] = _clone_with_insert(block, pos, key, value)
            else:
                pairs = list(block.pairs())
                pairs.insert(pos, (key, value))
                mid = len(pairs) // 2
                left = _block_from_pairs(pairs[:mid], self._block_capacity)
                right = _block_from_pairs(pairs[mid:], self._block_capacity)
                new_blocks[i : i + 1] = [left, right]
            self._publish(new_blocks, version.size + 1)

    __setitem__ = put

    def delete(self, key):
        """Remove ``key``; raises KeyError if absent."""
        with self._lock:
            version = self._version
            blocks, starts = version.blocks, version.starts
            found, block, pos, i = self._locate_in(starts, blocks, key)
            if not found:
                raise KeyError(key)
            self._delete_at(blocks, i, block, pos, version.size)

    def discard(self, key):
        """Remove ``key`` if present; return whether it was removed."""
        with self._lock:
            version = self._version
            blocks, starts = version.blocks, version.starts
            found, block, pos, i = self._locate_in(starts, blocks, key)
            if not found:
                return False
            self._delete_at(blocks, i, block, pos, version.size)
            return True

    def _delete_at(self, blocks, i, block, pos, size):
        # Point deletes keep the block's slot capacity: freed slots stay
        # available for future inserts (visible as fragmentation).
        if block.count == 1:
            new_blocks = blocks[:i] + blocks[i + 1 :]
        else:
            new_blocks = list(blocks)
            new_blocks[i] = _clone_without(block, pos)
        self._publish(new_blocks, size - 1)

    def delete_range(self, lo, hi):
        """Delete every key in the half-open interval [lo, hi).

        Returns the number of removed entries. Fully covered blocks are
        dropped wholesale (their capacity is reclaimed); partially
        covered edge blocks are rebuilt exact-fit.
        """
        if not lo < hi:
            return 0
        with self._lock:
            version = self._version
            blocks = version.blocks
            if not blocks:
                return 0
            new_blocks = []
            removed = 0
            changed = False
            for block in blocks:
                first = block.keys[0]
                last = block.keys[block.count - 1]
                if last < lo or first >= hi:
                    new_blocks.append(block)  # disjoint: keep as-is
                elif first >= lo and last < hi:
                    removed += block.count  # fully covered: drop wholesale
                    changed = True
                else:
                    survivors = [
                        (k, v)
                        for k, v in block.pairs()
                        if k < lo or k >= hi
                    ]
                    removed += block.count - len(survivors)
                    if survivors:
                        new_blocks.append(_block_from_pairs(survivors))
                    changed = True
            if changed:
                self._publish(new_blocks, version.size - removed)
            return removed
