"""A small thread-safe in-memory row store used by the paginator.

The store mimics a database table that keeps changing while pages are
being fetched: rows can be inserted, updated and deleted at any time
(thread-safe). Deletions leave a *tombstone* (bounded in number) so the
paginator can report which records disappeared inside a scanned range.

For every sort spec that has been queried, the store maintains a sorted
index incrementally (copy-on-write): each mutation produces new sorted
lists in O(n) pointer-copy time instead of a full O(n log n) re-sort,
and readers always see one consistent, immutable snapshot.
"""

import bisect
import threading
from collections import OrderedDict


class _SpecIndex:
    """Incrementally maintained sorted view for one sort spec."""

    __slots__ = ("spec", "keys", "rows", "tkeys", "tids")

    def __init__(self, spec):
        self.spec = spec
        self.keys = []   # sort tuples of live rows, ascending
        self.rows = []   # live row dicts, parallel to ``keys``
        self.tkeys = []  # sort tuples of tombstones, ascending
        self.tids = []   # tombstone ids, parallel to ``tkeys``


class Store:
    def __init__(self, tombstone_capacity=10000):
        self._rows = {}
        # id -> row snapshot taken at deletion time (FIFO-bounded)
        self._tombstones = OrderedDict()
        self._tombstone_capacity = tombstone_capacity
        self._version = 0
        self._lock = threading.RLock()
        self._indexes = {}  # spec fingerprint -> _SpecIndex

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------
    def insert(self, row):
        """Insert a row (a dict with a unique non-null ``id``)."""
        rid = row.get("id")
        if rid is None:
            raise ValueError("row must contain a non-null 'id'")
        with self._lock:
            if rid in self._rows:
                raise ValueError("duplicate id %r" % (rid,))
            row = dict(row)
            self._rows[rid] = row
            old_tomb = self._tombstones.pop(rid, None)  # re-insert resurrects
            for idx in self._indexes.values():
                if old_tomb is not None:
                    self._index_remove_tombstone(idx, old_tomb)
                self._index_add_live(idx, row)
            self._bump()

    def update(self, rid, changes):
        """Apply ``changes`` (a dict) to an existing row.

        Updating a sort-key column is allowed but semantically moves the
        row to a new position (equivalent to delete + insert there).
        """
        with self._lock:
            row = self._rows.get(rid)
            if row is None:
                raise KeyError("no row with id %r" % (rid,))
            if "id" in changes and changes["id"] != rid:
                raise ValueError("the 'id' column cannot be changed")
            old_tuples = {
                fp: idx.spec.row_sort_tuple(row)
                for fp, idx in self._indexes.items()
            }
            row.update(changes)
            for fp, idx in self._indexes.items():
                new_tuple = idx.spec.row_sort_tuple(row)
                if new_tuple != old_tuples[fp]:
                    self._index_remove_live(idx, old_tuples[fp], rid)
                    self._index_add_live(idx, row)
                # else: the row dict is shared with the index; already in place
            self._bump()

    def delete(self, rid):
        """Delete a row, keeping a tombstone. Returns True if it existed."""
        with self._lock:
            row = self._rows.pop(rid, None)
            if row is None:
                return False
            self._tombstones[rid] = row
            self._tombstones.move_to_end(rid)
            evicted = None
            if len(self._tombstones) > self._tombstone_capacity:
                evicted = self._tombstones.popitem(last=False)[1]
            for idx in self._indexes.values():
                self._index_remove_live(idx, idx.spec.row_sort_tuple(row), rid)
                self._index_add_tombstone(idx, row)
                if evicted is not None:
                    self._index_remove_tombstone(idx, evicted)
            self._bump()
            return True

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def get(self, rid):
        with self._lock:
            row = self._rows.get(rid)
            return dict(row) if row is not None else None

    def __len__(self):
        with self._lock:
            return len(self._rows)

    @property
    def version(self):
        with self._lock:
            return self._version

    def live_ids(self):
        with self._lock:
            return list(self._rows)

    def snapshot(self, spec):
        """Consistent sorted view: (sort_tuples, rows, tomb_tuples, tomb_ids).

        ``rows[i]`` is the live row whose sort tuple is ``sort_tuples[i]``;
        ``tomb_ids[i]`` is the deleted id whose tombstone sort tuple is
        ``tomb_tuples[i]``. Both key lists are sorted ascending. The
        returned lists are immutable snapshots owned by the store;
        callers must not mutate them.
        """
        fp = spec.fingerprint()
        with self._lock:
            idx = self._indexes.get(fp)
            if idx is None:
                idx = _SpecIndex(spec)
                pairs = sorted(
                    (spec.row_sort_tuple(row), row) for row in self._rows.values()
                )
                idx.keys = [p[0] for p in pairs]
                idx.rows = [p[1] for p in pairs]
                tpairs = sorted(
                    (spec.row_sort_tuple(row), rid)
                    for rid, row in self._tombstones.items()
                )
                idx.tkeys = [p[0] for p in tpairs]
                idx.tids = [p[1] for p in tpairs]
                self._indexes[fp] = idx
            return idx.keys, idx.rows, idx.tkeys, idx.tids

    # ------------------------------------------------------------------
    # index maintenance (copy-on-write; callers hold the lock)
    # ------------------------------------------------------------------
    @staticmethod
    def _index_add_live(idx, row):
        st = idx.spec.row_sort_tuple(row)
        pos = bisect.bisect_left(idx.keys, st)
        keys = idx.keys.copy()
        rows = idx.rows.copy()
        keys.insert(pos, st)
        rows.insert(pos, row)
        idx.keys = keys
        idx.rows = rows

    @staticmethod
    def _index_remove_live(idx, sort_tuple, rid):
        pos = bisect.bisect_left(idx.keys, sort_tuple)
        if pos >= len(idx.rows) or idx.rows[pos]["id"] != rid:
            raise RuntimeError("store index inconsistency for id %r" % (rid,))
        keys = idx.keys.copy()
        rows = idx.rows.copy()
        del keys[pos]
        del rows[pos]
        idx.keys = keys
        idx.rows = rows

    @staticmethod
    def _index_add_tombstone(idx, row):
        st = idx.spec.row_sort_tuple(row)
        pos = bisect.bisect_left(idx.tkeys, st)
        tkeys = idx.tkeys.copy()
        tids = idx.tids.copy()
        tkeys.insert(pos, st)
        tids.insert(pos, row["id"])
        idx.tkeys = tkeys
        idx.tids = tids

    @staticmethod
    def _index_remove_tombstone(idx, tomb_row):
        st = idx.spec.row_sort_tuple(tomb_row)
        pos = bisect.bisect_left(idx.tkeys, st)
        if pos >= len(idx.tids) or idx.tids[pos] != tomb_row["id"]:
            raise RuntimeError(
                "store tombstone index inconsistency for id %r" % (tomb_row["id"],)
            )
        tkeys = idx.tkeys.copy()
        tids = idx.tids.copy()
        del tkeys[pos]
        del tids[pos]
        idx.tkeys = tkeys
        idx.tids = tids

    # ------------------------------------------------------------------
    def _bump(self):
        self._version += 1
