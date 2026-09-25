"""In-memory index supporting mutation during traversal.

Iteration semantics: SNAPSHOT (see docs/ITERATION_SEMANTICS.md).
An iterator created at time T yields exactly the records that were active
at T, each exactly once, regardless of any later put/delete/cleanup.

Fixes relative to memory_index_buggy.py:
  1. delete() marks a tombstone instead of shifting slots, so positional
     state is never invalidated; iterators use snapshots anyway.
  2. Re-inserted keys reuse freed slots and never affect live iterators.
  3. cleanup() only replaces the index's own storage; iterators hold their
     own snapshot buffers, so they can never observe released storage.
  4. _size/_tombstones are maintained on every mutation and checked by
     check_invariants(); stats() is consistent at all times.
"""

_TOMBSTONE = object()  # sentinel for deleted slots


class _Entry:
    __slots__ = ("key", "value")

    def __init__(self, key, value):
        self.key = key
        self.value = value


class MemoryIndex:
    """Not thread-safe; use external synchronization for shared access."""

    def __init__(self):
        self._slots = []        # list[_Entry | _TOMBSTONE]
        self._index = {}        # key -> slot position
        self._free = []         # positions of tombstone slots, reusable by put()
        self._size = 0          # number of active records
        self._tombstones = 0    # number of deleted-but-not-compacted slots
        self._live_iterators = 0

    # ------------------------------------------------------------------
    # mutation
    # ------------------------------------------------------------------
    def put(self, key, value):
        pos = self._index.get(key)
        if pos is not None:
            self._slots[pos].value = value
            return
        if self._free:
            pos = self._free.pop()
            self._slots[pos] = _Entry(key, value)
            self._tombstones -= 1
        else:
            pos = len(self._slots)
            self._slots.append(_Entry(key, value))
        self._index[key] = pos
        self._size += 1

    def delete(self, key):
        pos = self._index.pop(key)  # raises KeyError for missing keys
        self._slots[pos] = _TOMBSTONE
        self._free.append(pos)
        self._size -= 1
        self._tombstones += 1

    def cleanup(self):
        """Compact storage, releasing tombstone slots.

        Safe while iterators exist: snapshot iterators are fully
        independent of this storage and keep working per the snapshot
        semantics.
        """
        if self._tombstones == 0:
            return
        self._slots = [e for e in self._slots if e is not _TOMBSTONE]
        self._index = {e.key: i for i, e in enumerate(self._slots)}
        self._free.clear()
        self._tombstones = 0
        self.check_invariants()

    # ------------------------------------------------------------------
    # lookup
    # ------------------------------------------------------------------
    def get(self, key):
        return self._slots[self._index[key]].value

    def __contains__(self, key):
        return key in self._index

    def __len__(self):
        return self._size

    # ------------------------------------------------------------------
    # statistics & invariants
    # ------------------------------------------------------------------
    def stats(self):
        """Consistent at any moment: capacity == size + deleted."""
        return {
            "size": self._size,
            "deleted": self._tombstones,
            "capacity": len(self._slots),
        }

    def check_invariants(self):
        assert self._size == len(self._index), "size != live key count"
        assert self._tombstones == len(self._free), "tombstones != free slots"
        assert self._size + self._tombstones == len(self._slots), (
            "capacity != size + deleted"
        )
        live = 0
        for entry in self._slots:
            if entry is not _TOMBSTONE:
                live += 1
        assert live == self._size, "live slots != size"
        for key, pos in self._index.items():
            entry = self._slots[pos]
            assert entry is not _TOMBSTONE and entry.key == key, (
                "index points at wrong slot"
            )

    # ------------------------------------------------------------------
    # iteration
    # ------------------------------------------------------------------
    def __iter__(self):
        return SnapshotIterator(self)

    @property
    def active_iterators(self):
        """Number of iterators currently holding a snapshot buffer."""
        return self._live_iterators


class SnapshotIterator:
    """Freezes the visible (key, value) set at creation time.

    The snapshot buffer is released as soon as the iterator is exhausted
    or explicitly closed (also usable as a context manager).
    """

    def __init__(self, index):
        self._index = index
        self._snapshot = [
            (entry.key, entry.value)
            for entry in index._slots
            if entry is not _TOMBSTONE
        ]
        self._pos = 0
        self._closed = False
        index._live_iterators += 1

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed or self._pos >= len(self._snapshot):
            self.close()
            raise StopIteration
        item = self._snapshot[self._pos]
        self._pos += 1
        return item

    def close(self):
        """Release the snapshot buffer and unregister from the index."""
        if not self._closed:
            self._snapshot = []
            self._closed = True
            self._index._live_iterators -= 1

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def __del__(self):  # backstop; exhaustion/close() is the normal path
        try:
            self.close()
        except Exception:
            pass
