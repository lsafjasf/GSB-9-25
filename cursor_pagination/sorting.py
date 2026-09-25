"""Multi-column sort specification and total-order key computation.

Ordering rules (a total order over rows):
  * columns are compared left to right, each with its own direction;
  * ``None`` (NULL) always sorts LAST, for both ASC and DESC columns;
  * rows that are equal on every sort column are ordered by their ``id``
    column (ascending), which must be unique and non-null. This makes the
    order total and therefore stable under duplicate sort keys.
"""

import hashlib

ASC = "asc"
DESC = "desc"
_DIRECTIONS = (ASC, DESC)

_ID_COLUMN = "id"


class _Desc:
    """Wrapper reversing the comparison of a value (for DESC columns)."""

    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def __lt__(self, other):
        return other.v < self.v

    def __le__(self, other):
        return not self.v < other.v

    def __gt__(self, other):
        return self.v < other.v

    def __ge__(self, other):
        return not other.v < self.v

    def __eq__(self, other):
        return isinstance(other, _Desc) and self.v == other.v

    def __hash__(self):
        return hash(self.v)


class SortSpec:
    """Immutable description of the sort order of a listing.

    ``columns`` is an iterable of ``(column_name, direction)`` pairs where
    direction is ``"asc"`` or ``"desc"``. The reserved column ``id`` is
    appended automatically as the final ascending tie-breaker.
    """

    __slots__ = ("columns",)

    def __init__(self, columns):
        cols = tuple(columns)
        if not cols:
            raise ValueError("SortSpec requires at least one sort column")
        names = []
        for col in cols:
            if not isinstance(col, (tuple, list)) or len(col) != 2:
                raise ValueError("each sort column must be a (name, direction) pair")
            name, direction = col
            if not isinstance(name, str) or not name:
                raise ValueError("sort column name must be a non-empty string")
            if direction not in _DIRECTIONS:
                raise ValueError("direction must be 'asc' or 'desc', got %r" % (direction,))
            names.append(name)
        if _ID_COLUMN in names:
            raise ValueError("%r is reserved as the tie-breaker column" % _ID_COLUMN)
        if len(set(names)) != len(names):
            raise ValueError("duplicate sort column names: %r" % (names,))
        self.columns = tuple((name, direction) for name, direction in cols)

    def __repr__(self):
        return "SortSpec(%r)" % (self.columns,)

    def __eq__(self, other):
        return isinstance(other, SortSpec) and self.columns == other.columns

    def __hash__(self):
        return hash(self.columns)

    def fingerprint(self):
        """Short stable hash of the sort spec, embedded in signed cursors."""
        h = hashlib.sha256()
        for name, direction in self.columns:
            h.update(name.encode("utf-8"))
            h.update(b"\x00")
            h.update(direction.encode("ascii"))
            h.update(b"\x01")
        return h.hexdigest()[:16]

    def key_values(self, row):
        """Raw sort-key values of a row: sort columns followed by ``id``."""
        return tuple(row.get(name) for name, _ in self.columns) + (row[_ID_COLUMN],)

    def sort_tuple(self, values):
        """Transform raw key values into a plain tuple with the desired
        total order under normal tuple comparison.

        Each element becomes ``(rank, transformed)`` where ``rank`` is 1 for
        None (so NULLs sort last) and 0 otherwise; DESC values are wrapped
        in :class:`_Desc`. The trailing ``id`` element is plain ascending.
        """
        out = []
        for i, (_, direction) in enumerate(self.columns):
            v = values[i]
            if v is None:
                out.append((1, None))
            elif direction == ASC:
                out.append((0, v))
            else:
                out.append((0, _Desc(v)))
        out.append((0, values[-1]))
        return tuple(out)

    def row_sort_tuple(self, row):
        return self.sort_tuple(self.key_values(row))
