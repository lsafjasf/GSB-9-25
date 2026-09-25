"""MemoryIndex: an in-memory hash index that supports mutation during traversal.

Iteration semantics: SNAPSHOT (see SEMANTICS.md).

* ``items()`` / ``keys()`` / ``__iter__`` return an iterator over an immutable
  snapshot of the live (key, value) pairs taken at call time.
* Inserts, updates, deletes and compactions performed afterwards are invisible
  to that iterator; deleted values are released by the live table but remain
  referenced by the snapshot until the iterator is exhausted or closed.
* Each live record in the snapshot is therefore visited exactly once: no
  skips, no duplicates, and no reads of freed buffers (the snapshot owns its
  own tuple array).
* Snapshot buffers are released deterministically when iteration finishes,
  when ``close()`` is called, or when the iterator is garbage collected, and
  iterators support the context-manager protocol.
* Every mutating operation keeps the counters in :meth:`MemoryIndex.stats`
  consistent with the table; ``check_invariants()`` verifies them against a
  full table scan and can be enabled automatically for debugging.

Only the Python standard library is used.
"""

import threading
import weakref

__all__ = ["MemoryIndex", "SnapshotIterator", "IteratorClosedError"]

_EMPTY = object()
_TOMBSTONE = object()


class IteratorClosedError(RuntimeError):
    """Raised when a closed snapshot iterator is advanced explicitly."""


class SnapshotIterator:
    """Iterator over an immutable snapshot captured by :meth:`MemoryIndex.items`.

    The snapshot is a private ``(keys, values)`` tuple pair owned by this
    object.  It is independent of the index's live arrays, so rebuilding,
    compaction or deletion can never expose released data through it.
    """

    __slots__ = ("_keys", "_values", "_cursor", "_closed", "_on_release", "__weakref__")

    def __init__(self, snapshot_keys, snapshot_values, on_release=None):
        self._keys = snapshot_keys
        self._values = snapshot_values
        self._cursor = 0
        self._closed = False
        self._on_release = on_release

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise IteratorClosedError("snapshot iterator has been closed")
        cursor = self._cursor
        if cursor >= len(self._keys):
            self.close()
            raise StopIteration
        item = (self._keys[cursor], self._values[cursor])
        self._cursor = cursor + 1
        return item

    def close(self):
        """Release the snapshot buffers. Safe to call repeatedly."""
        if self._closed:
            return
        self._closed = True
        # Drop references so user values become collectable immediately.
        self._keys = ()
        self._values = ()
        callback = self._on_release
        if callback is not None:
            self._on_release = None
            callback(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __del__(self):
        # Interpreter shutdown may have stripped globals; guard everything.
        try:
            self.close()
        except Exception:
            pass


class MemoryIndex:
    """Open-addressing hash index (linear probing) with snapshot iterators."""

    _LOAD_FACTOR = 0.7
    _TOMBSTONE_RATIO = 0.25
    _MIN_CAPACITY = 8

    def __init__(self, capacity=16, *, check_invariants=False):
        capacity = max(self._MIN_CAPACITY, int(capacity))
        self._capacity = self._next_power_of_two(capacity)
        self._slots = [_EMPTY] * self._capacity
        self._values = [None] * self._capacity
        self._entries = 0
        self._tombstones = 0
        self._lock = threading.RLock()
        self._live_iterators = weakref.WeakSet()
        self._debug_check = bool(check_invariants)

    # -- container helpers -------------------------------------------------

    @staticmethod
    def _next_power_of_two(value):
        power = 1
        while power < value:
            power <<= 1
        return power

    def _index(self, key):
        return hash(key) & (self._capacity - 1)

    def _find_slot(self, key):
        """Locate ``key`` (status ``present``) or its insert position.

        Returns ``(status, slot)`` where status is one of:

        * ``present``: the slot currently stores ``key``;
        * ``tombstone``: key is absent and ``slot`` is the first tombstone
          passed while probing (preferred insertion slot);
        * ``empty``: key is absent and ``slot`` is an empty slot.
        """
        slots = self._slots
        mask = self._capacity - 1
        slot = hash(key) & mask
        first_tombstone = -1
        while True:
            state = slots[slot]
            if state is _EMPTY:
                if first_tombstone < 0:
                    return ("empty", slot)
                return ("tombstone", first_tombstone)
            if state is _TOMBSTONE:
                if first_tombstone < 0:
                    first_tombstone = slot
            elif state == key:
                return ("present", slot)
            slot = (slot + 1) & mask

    def _post_mutation_check(self):
        if self._debug_check:
            self.check_invariants()

    #-- writes -------------------------------------------------------------

    def insert(self, key, value):
        """Insert or replace ``key``; returns ``"inserted"`` or ``"updated"``."""
        with self._lock:
            status, slot = self._find_slot(key)
            if status == "present":
                self._values[slot] = value
                self._post_mutation_check()
                return "updated"
            self._slots[slot] = key
            self._values[slot] = value
            if status == "tombstone":
                # Reclaiming a tombstone adds a live record.
                self._tombstones -= 1
                self._entries += 1
            else:
                self._entries += 1
            self._maybe_rehash(grow=True)
            self._post_mutation_check()
            return "inserted"

    def __setitem__(self, key, value):
        self.insert(key, value)

    def delete(self, key):
        """Delete ``key``; returns True if a live record was removed."""
        with self._lock:
            status, slot = self._find_slot(key)
            if status != "present":
                return False
            self._slots[slot] = _TOMBSTONE
            self._values[slot] = None  # release user value promptly
            self._entries -= 1
            self._tombstones += 1
            self._maybe_rehash(grow=False)
            self._post_mutation_check()
            return True

    def __delitem__(self, key):
        if not self.delete(key):
            raise KeyError(key)

    def get(self, key, default=None):
        with self._lock:
            status, slot = self._find_slot(key)
            if status == "present":
                return self._values[slot]
            return default

    def __getitem__(self, key):
        with self._lock:
            status, slot = self._find_slot(key)
            if status == "present":
                return self._values[slot]
        raise KeyError(key)

    def __contains__(self, key):
        with self._lock:
            status, slot = self._find_slot(key)
            return status == "present"

    #-- iteration (snapshot semantics) -------------------------------------

    def _make_snapshot(self):
        with self._lock:
            keys = tuple(
                self._slots[slot]
                for slot in range(self._capacity)
                if self._slots[slot] is not _EMPTY
                and self._slots[slot] is not _TOMBSTONE
            )
            values = tuple(
                self._values[slot]
                for slot in range(self._capacity)
                if self._slots[slot] is not _EMPTY
                and self._slots[slot] is not _TOMBSTONE
            )
            iterator = SnapshotIterator(keys, values, self._release_iterator)
            self._live_iterators.add(iterator)
            return iterator

    def items(self):
        """Snapshot the live pairs and iterate them exactly once."""
        return self._make_snapshot()

    def keys(self):
        """Snapshot the live keys."""
        return _SnapshotKeys(self._make_snapshot())

    def values(self):
        """Snapshot the live values."""
        return _SnapshotValues(self._make_snapshot())

    def __iter__(self):
        return _SnapshotKeys(self._make_snapshot())

    def _release_iterator(self, iterator):
        # WeakSet.discard is harmless if the iterator was already gone.
        self._live_iterators.discard(iterator)

    def active_iterators(self):
        """Number of currently open snapshot iterators."""
        with self._lock:
            return sum(1 for _ in self._live_iterators)

    #-- maintenance ---------------------------------------------------------

    def compact(self):
        """Rebuild into a right-sized table; existing snapshots stay valid."""
        with self._lock:
            self._rebuild(self._table_capacity_for(self._entries))
            self._post_mutation_check()
            return self._capacity

    def clear(self):
        with self._lock:
            for slot in range(self._capacity):
                self._slots[slot] = _EMPTY
                self._values[slot] = None
            self._entries = 0
            self._tombstones = 0
            self._post_mutation_check()

    def _table_capacity_for(self, entries):
        needed = self._next_power_of_two(
            max(self._MIN_CAPACITY, int(entries / self._LOAD_FACTOR) + 1)
        )
        return needed

    def _maybe_rehash(self, *, grow):
        occupied = self._entries + self._tombstones
        if grow and self._entries + 1 > self._capacity * self._LOAD_FACTOR:
            self._rebuild(self._capacity << 1)
        elif self._tombstones > self._capacity * self._TOMBSTONE_RATIO and occupied:
            self._rebuild(self._capacity)
        elif (
            not grow
            and self._capacity > self._MIN_CAPACITY
            and occupied < self._capacity * 0.2
        ):
            self._rebuild(self._table_capacity_for(self._entries))

    def _rebuild(self, new_capacity):
        old_slots = self._slots
        old_values = self._values
        new_slots = [_EMPTY] * new_capacity
        new_values = [None] * new_capacity
        mask = new_capacity - 1
        for old_slot in range(len(old_slots)):
            key = old_slots[old_slot]
            if key is _EMPTY or key is _TOMBSTONE:
                continue
            slot = hash(key) & mask
            while new_slots[slot] is not _EMPTY:
                slot = (slot + 1) & mask
            new_slots[slot] = key
            new_values[slot] = old_values[old_slot]
        self._slots = new_slots
        self._values = new_values
        self._capacity = new_capacity
        self._tombstones = 0
        # Old arrays are dropped here; any open snapshot keeps its own tuples,
        # never these arrays.

    #-- statistics / invariants --------------------------------------------

    def stats(self):
        with self._lock:
            return {
                "entries": self._entries,
                "deleted": self._tombstones,
                "tombstones": self._tombstones,
                "capacity": self._capacity,
                "load": (self._entries + self._tombstones) / self._capacity,
                "active_iterators": sum(1 for _ in self._live_iterators),
            }

    @property
    def deleted_count(self):
        return self._tombstones

    @property
    def capacity(self):
        return self._capacity

    def __len__(self):
        return self._entries

    def check_invariants(self):
        """Assert counters and table state agree. Returns the stats mapping."""
        with self._lock:
            live = 0
            tombstones = 0
            seen = set()
            for slot in range(self._capacity):
                state = self._slots[slot]
                if state is _EMPTY:
                    continue
                if state is _TOMBSTONE:
                    tombstones += 1
                    assert self._values[slot] is None, (
                        "tombstone must not retain a value at slot %d" % slot
                    )
                else:
                    live += 1
                    assert state not in seen, "duplicate key %r" % (state,)
                    seen.add(state)
            assert live == self._entries, (
                "entries counter %d != scanned live records %d"
                % (self._entries, live)
            )
            assert tombstones == self._tombstones, (
                "tombstone counter %d != scanned tombstones %d"
                % (self._tombstones, tombstones)
            )
            assert len(self._slots) == self._capacity
            assert len(self._values) == self._capacity
            assert self._capacity >= self._MIN_CAPACITY
            assert self._entries + self._tombstones <= self._capacity
            assert self._capacity & (self._capacity - 1) == 0
            return self.stats()


class _SnapshotKeys:
    __slots__ = ("_iterator",)

    def __init__(self, iterator):
        self._iterator = iterator

    def __iter__(self):
        return self

    def __next__(self):
        key, _ = next(self._iterator)
        return key

    def close(self):
        self._iterator.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class _SnapshotValues:
    __slots__ = ("_iterator",)

    def __init__(self, iterator):
        self._iterator = iterator

    def __iter__(self):
        return self

    def __next__(self):
        _, value = next(self._iterator)
        return value

    def close(self):
        self._iterator.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
