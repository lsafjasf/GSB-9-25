"""Keyset (cursor-based) pagination over a Store.

Semantics while data mutates between page fetches:

  * a row already emitted in an earlier page is never emitted again
    (the cursor is a position in the sort order, not an offset);
  * a live row whose sort position has not been passed yet is guaranteed
    to appear in a later page, regardless of concurrent inserts elsewhere;
  * a deleted row simply disappears from ``items``; if its tombstone is
    still retained and its sort position falls inside the range scanned
    by a page, its id is reported in ``Page.deleted``;
  * updating a non-key column does not move the row; updating a sort-key
    column is equivalent to delete + insert at the new position.
"""

import bisect

from .cursor import BACKWARD, FORWARD, Cursor, decode_cursor, encode_cursor
from .errors import CursorDecodeError


class Page:
    """One page of results.

    Attributes:
        items: list of row dicts in sort order.
        deleted: ids of rows deleted inside this page's scanned range
            (only while their tombstones are retained).
        next_cursor / prev_cursor: opaque signed cursor tokens, or None
            when there is currently no further page in that direction.
        has_next / has_prev: whether more rows currently exist beyond
            this page in that direction.
    """

    __slots__ = ("items", "deleted", "next_cursor", "prev_cursor", "has_next", "has_prev")

    def __init__(self, items, deleted, next_cursor, prev_cursor, has_next, has_prev):
        self.items = items
        self.deleted = deleted
        self.next_cursor = next_cursor
        self.prev_cursor = prev_cursor
        self.has_next = has_next
        self.has_prev = has_prev

    def __repr__(self):
        return "Page(items=%d, deleted=%d, has_next=%r, has_prev=%r)" % (
            len(self.items),
            len(self.deleted),
            self.has_next,
            self.has_prev,
        )


class Paginator:
    """Issues pages for one (store, sort spec, page size) combination.

    ``secret`` signs every cursor; keep it server-side. Pages fetched with
    a tampered cursor raise :class:`CursorTamperedError`.
    """

    def __init__(self, store, sort_spec, page_size=50, secret=None):
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        if secret is None:
            raise ValueError("a secret is required to sign cursors")
        self._store = store
        self._spec = sort_spec
        self._page_size = page_size
        self._secret = secret

    # ------------------------------------------------------------------
    def first_page(self):
        """Return the first page of the listing."""
        return self.page()

    def page(self, cursor_token=None):
        """Fetch the page at ``cursor_token`` (None = first page)."""
        keys, rows, tkeys, tids = self._store.snapshot(self._spec)
        n = len(rows)

        if cursor_token is None:
            direction = FORWARD
            skey = None
        else:
            cur = decode_cursor(cursor_token, self._spec, self._secret)
            direction = cur.direction
            skey = self._spec.sort_tuple(cur.keys)

        if direction == FORWARD:
            start = 0 if skey is None else bisect.bisect_right(keys, skey)
            end = min(start + self._page_size, n)
            window = rows[start:end]
            has_next = end < n
            has_prev = start > 0
            if window:
                lo = 0 if skey is None else bisect.bisect_right(tkeys, skey)
                hi = bisect.bisect_right(tkeys, keys[end - 1])
                deleted = tids[lo:hi]
            else:
                deleted = []
        else:
            if skey is None:
                raise CursorDecodeError("backward pagination requires a cursor")
            end = bisect.bisect_left(keys, skey)
            start = max(0, end - self._page_size)
            window = rows[start:end]
            has_prev = start > 0
            has_next = end < n
            if window:
                lo = bisect.bisect_left(tkeys, keys[start])
                hi = bisect.bisect_left(tkeys, skey)
                deleted = tids[lo:hi]
            else:
                deleted = []

        items = [dict(r) for r in window]
        next_cursor = (
            self._encode(self._spec.key_values(window[-1]), FORWARD)
            if window and has_next
            else None
        )
        prev_cursor = (
            self._encode(self._spec.key_values(window[0]), BACKWARD)
            if window and has_prev
            else None
        )
        return Page(items, deleted, next_cursor, prev_cursor, has_next, has_prev)

    def next_page(self, page):
        """Fetch the page after ``page`` (None if there is none)."""
        if page.next_cursor is None:
            return None
        return self.page(page.next_cursor)

    def prev_page(self, page):
        """Fetch the page before ``page`` (None if there is none)."""
        if page.prev_cursor is None:
            return None
        return self.page(page.prev_cursor)

    # ------------------------------------------------------------------
    def _encode(self, key_values, direction):
        return encode_cursor(Cursor(key_values, direction), self._spec, self._secret)
