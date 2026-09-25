"""Buggy in-memory index (original defective implementation).

Kept ONLY to reproduce the four production issues in tests/test_buggy_repro.py.
Do not use. See memory_index.py for the fixed implementation and
docs/ITERATION_SEMANTICS.md for the iteration semantics.

Known defects (reproduced by tests):
  1. delete() physically pops the slot, shifting successors; a live iterator
     holding a positional cursor then skips the record after the deleted one.
  2. delete()+put() of the same key appends a new slot at the end, so a live
     iterator visits the key a second time.
  3. cleanup() replaces the slot array; iterators that captured the old array
     keep reading detached/stale (already deleted) records.
  4. delete() never decrements _size; the count drifts from reality until a
     cleanup() happens to recompute it.
"""


class BuggyMemoryIndex:
    def __init__(self):
        self._entries = []   # dense list of [key, value]
        self._index = {}     # key -> position in _entries
        self._size = 0
        self._deleted = 0

    def put(self, key, value):
        if key in self._index:
            self._entries[self._index[key]][1] = value
            return
        self._index[key] = len(self._entries)
        self._entries.append([key, value])
        self._size += 1

    def get(self, key):
        return self._entries[self._index[key]][1]

    def __contains__(self, key):
        return key in self._index

    def delete(self, key):
        pos = self._index.pop(key)
        self._entries.pop(pos)  # BUG 1: shifts successors under live cursors
        for i in range(pos, len(self._entries)):
            self._index[self._entries[i][0]] = i
        self._deleted += 1
        # BUG 4: self._size is never decremented here.

    def cleanup(self):
        """Reallocate the slot array to release capacity."""
        self._entries = self._entries[:]  # BUG 3: detaches arrays captured by live iterators
        self._index = {e[0]: i for i, e in enumerate(self._entries)}
        self._size = len(self._entries)  # silently masks BUG 4 until the next delete
        self._deleted = 0

    def __len__(self):
        return self._size

    def stats(self):
        return {
            "size": self._size,
            "deleted": self._deleted,
            "capacity": len(self._entries),
        }

    def __iter__(self):
        return BuggyIterator(self)


class BuggyIterator:
    def __init__(self, index):
        self._entries = index._entries  # BUG 3: captures the mutable internal array
        self._pos = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._pos >= len(self._entries):
            raise StopIteration
        entry = self._entries[self._pos]
        self._pos += 1
        return entry[0], entry[1]
