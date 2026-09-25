"""Legacy in-memory index kept ONLY to reproduce the production defects.

This module intentionally contains four classes of bugs.  Do not use it for
anything except the regression harness that proves those bugs existed:

1. Iteration walks a live parallel-array table and the current slot is removed
   physically during traversal, so the record shifting into the cursor slot is
   skipped (deleted-current-record drops the next record).
2. Reinserting a deleted key while an iterator is alive can visit that key
   twice.
3. compact() frees the backing arrays but old iterators still reference the
   released buffers ("use-after-free" style stale reads).
4. The statistics counters (entry count / deleted count / capacity) drift away
   from reality.
"""

_FREED = "<FREED-BUFFER>"


class _BuggyIterator:
    """Live iterator: it reads the table directly instead of a snapshot."""

    def __init__(self, index):
        self._index = index
        self._keys = index._keys          # bound straight to the live buffer
        self._values = index._values      # same here
        self._cursor = 0
        self._seen = set()

    def __iter__(self):
        return self

    def __next__(self):
        table = self._keys
        if table is None:                 # whole index was clear()ed
            raise StopIteration
        while self._cursor < len(table):
            slot = self._cursor
            key = table[slot]
            if key is not None:
                if key == _FREED:
                    raise RuntimeError(
                        "stale iterator read a released (freed) slot"
                    )
                self._cursor += 1
                value = self._values[slot]
                if value is _FREED or value == _FREED:
                    # BUG #3: compact() reused/freed the buffer under us.
                    raise RuntimeError(
                        "stale iterator read released record for key %r" % (key,)
                    )
                if key in self._seen:
                    # BUG #2 surface form: a key visited again after reinsert.
                    raise AssertionError(
                        "key %r visited twice during iteration" % (key,)
                    )
                self._seen.add(key)
                return key, value
            self._cursor += 1
        raise StopIteration


class BuggyMemoryIndex:
    """Naive parallel-array hash index (the pre-fix production code)."""

    def __init__(self, capacity=16):
        self._keys = [None] * capacity
        self._values = [None] * capacity
        self._entries = 0
        # BUG #4 seed: "deleted" is a forever-accumulating counter and the
        # entry counter tracks allocations rather than live records.
        self._deleted = 0

    # -- basic API ---------------------------------------------------------

    def insert(self, key, value):
        for slot in range(len(self._keys)):
            if self._keys[slot] == key:
                self._values[slot] = value
                # BUG #4: replacing an existing record inflates entry count.
                self._entries += 1
                return
        for slot in range(len(self._keys)):
            if self._keys[slot] is None:
                self._keys[slot] = key
                self._values[slot] = value
                self._entries += 1
                return
        self._grow()
        self.insert(key, value)

    def delete(self, key):
        position = None
        for slot in range(len(self._keys)):
            if self._keys[slot] == key:
                position = slot
                break
        if position is None:
            return False
        # BUG #1: physically remove the slot and shift the tail left; a live
        # iterator sitting at this cursor loses the record shifted into it.
        last = len(self._keys) - 1
        for slot in range(position, last):
            self._keys[slot] = self._keys[slot + 1]
            self._values[slot] = self._values[slot + 1]
        self._keys[last] = None
        self._values[last] = None
        self._deleted += 1
        return True

    def get(self, key, default=None):
        for slot in range(len(self._keys)):
            if self._keys[slot] == key:
                return self._values[slot]
        return default

    def items(self):
        return _BuggyIterator(self)

    def __iter__(self):
        for key, _ in self.items():
            yield key

    def __contains__(self, key):
        return any(slot == key for slot in self._keys)

    def compact(self):
        # BUG #3: free the old buffers (values are overwritten with a sentinel
        # and a fresh array is installed) while live iterators keep the old
        # reference.
        old_keys = self._keys
        old_values = self._values
        live = [
            (old_keys[slot], old_values[slot])
            for slot in range(len(old_keys))
            if old_keys[slot] is not None
        ]
        new_capacity = max(8, len(live) * 2)
        self._keys = [None] * new_capacity
        self._values = [None] * new_capacity
        for key, value in live:
            self.insert(key, value)
        for slot in range(len(old_keys)):
            # Poison both released buffers so a stale iterator reads a
            # recognisable freed marker instead of silently ending early.
            old_keys[slot] = _FREED
            old_values[slot] = _FREED
        # BUG #4 bonus: the counter of deleted records is never reset even
        # though the tombstones were physically removed.
        return len(live)

    def clear(self):
        old_keys = self._keys
        old_values = self._values
        for slot in range(len(old_values)):
            old_keys[slot] = _FREED
            old_values[slot] = _FREED
        self._keys = [None] * len(self._keys)
        self._values = [None] * len(self._values)
        self._entries = 0
        self._deleted = 0

    def _grow(self):
        new_keys = [None] * (len(self._keys) * 2)
        new_values = [None] * (len(self._values) * 2)
        new_keys[: len(self._keys)] = self._keys
        new_values[: len(self._values)] = self._values
        self._keys = new_keys
        self._values = new_values

    # -- statistics --------------------------------------------------------

    def stats(self):
        # BUG #4: reports the allocation-time counters verbatim.
        capacity = len(self._keys)
        return {
            "entries": self._entries,
            "deleted": self._deleted,
            "capacity": capacity,
            "tombstones": self._deleted,
        }

    def __len__(self):
        return self._entries
