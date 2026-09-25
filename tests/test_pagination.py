"""Pagination semantics: ordering, stability, nulls, edges, round trips,
and deterministic mutation interleaving (insert/update/delete mid-paging).
"""

import unittest

from cursor_pagination import ASC, DESC, Paginator, SortSpec, Store

SECRET = "semantics-secret"


def make_store(rows):
    store = Store()
    for row in rows:
        store.insert(row)
    return store


def all_ids(paginator):
    """Drain forward from the first page, returning (ids, deleted_lists)."""
    page = paginator.first_page()
    seen, deleted = [], []
    while page is not None:
        seen.extend(r["id"] for r in page.items)
        deleted.extend(page.deleted)
        page = paginator.next_page(page)
    return seen, deleted


class OrderingTest(unittest.TestCase):
    def test_multi_column_order_with_duplicate_tiebreaker(self):
        rows = [
            {"id": i, "a": v, "b": (i % 3), "payload": "x" * 100}
            for i, v in enumerate([1, 2, 2, 2, 3, 1] * 2)
        ]
        paginator = Paginator(
            make_store(rows), SortSpec([("a", ASC), ("b", DESC)]), page_size=4,
            secret=SECRET,
        )
        ids, _ = all_ids(paginator)
        expected = [
            r["id"]
            for r in sorted(rows, key=lambda r: (r["a"], -r["b"], r["id"]))
        ]
        self.assertEqual(ids, expected)
        self.assertEqual(len(ids), len(rows))

    def test_desc_then_asc_mix(self):
        rows = [{"id": i, "a": i % 2, "b": i} for i in range(6)]
        paginator = Paginator(
            make_store(rows), SortSpec([("a", DESC), ("b", ASC)]), page_size=2,
            secret=SECRET,
        )
        ids, _ = all_ids(paginator)
        expected = [r["id"] for r in sorted(rows, key=lambda r: (-r["a"], r["b"]))]
        self.assertEqual(ids, expected)


class NullTest(unittest.TestCase):
    def test_nulls_sort_last_under_asc(self):
        rows = [
            {"id": 3, "a": None},
            {"id": 1, "a": 10},
            {"id": 4, "a": None},
            {"id": 2, "a": 5},
        ]
        paginator = Paginator(
            make_store(rows), SortSpec([("a", ASC)]), page_size=10, secret=SECRET
        )
        ids, _ = all_ids(paginator)
        self.assertEqual(ids, [2, 1, 3, 4])  # non-null asc, nulls last by id

    def test_nulls_sort_last_under_desc_and_tiebreak_on_second_column(self):
        rows = [
            {"id": 3, "a": None, "b": 30},
            {"id": 1, "a": 10, "b": 1},
            {"id": 4, "a": None, "b": 10},
            {"id": 2, "a": 5, "b": 2},
        ]
        paginator = Paginator(
            make_store(rows), SortSpec([("a", DESC), ("b", ASC)]),
            page_size=10, secret=SECRET,
        )
        ids, _ = all_ids(paginator)
        # a desc: 10 then 5; nulls still last, ordered by b asc
        self.assertEqual(ids, [1, 2, 4, 3])


class PageEdgeTest(unittest.TestCase):
    def test_empty_result(self):
        paginator = Paginator(Store(), SortSpec([("a", ASC)]), secret=SECRET)
        page = paginator.first_page()
        self.assertEqual(page.items, [])
        self.assertEqual(page.deleted, [])
        self.assertFalse(page.has_next)
        self.assertFalse(page.has_prev)
        self.assertIsNone(page.next_cursor)
        self.assertIsNone(page.prev_cursor)

    def test_single_page(self):
        rows = [{"id": i, "a": i} for i in range(3)]
        page = Paginator(
            make_store(rows), SortSpec([("a", ASC)]), page_size=10, secret=SECRET
        ).first_page()
        self.assertEqual([r["id"] for r in page.items], [0, 1, 2])
        self.assertFalse(page.has_next)
        self.assertFalse(page.has_prev)

    def test_last_page_partial_and_cursors(self):
        rows = [{"id": i, "a": i} for i in range(10)]
        paginator = Paginator(
            make_store(rows), SortSpec([("a", ASC)]), page_size=4, secret=SECRET
        )
        page = paginator.first_page()
        sizes = []
        self.assertIsNone(page.prev_cursor)
        self.assertTrue(page.has_next)
        while page is not None:
            sizes.append(len(page.items))
            if page.next_cursor is None:
                self.assertFalse(page.has_next)
                break
            page = paginator.next_page(page)
        self.assertEqual(sizes, [4, 4, 2])

    def test_exact_multiple_pages(self):
        rows = [{"id": i, "a": i} for i in range(8)]
        paginator = Paginator(
            make_store(rows), SortSpec([("a", ASC)]), page_size=4, secret=SECRET
        )
        page = paginator.first_page()
        self.assertTrue(page.has_next)
        last = paginator.next_page(page)
        self.assertFalse(last.has_next)
        self.assertIsNone(last.next_cursor)
        self.assertTrue(last.has_prev)


class BackwardTest(unittest.TestCase):
    def test_forward_then_backward_returns_same_position(self):
        rows = [{"id": i, "a": i} for i in range(23)]
        paginator = Paginator(
            make_store(rows), SortSpec([("a", ASC)]), page_size=5, secret=SECRET
        )
        forward_pages = []
        page = paginator.first_page()
        while page is not None:
            forward_pages.append([r["id"] for r in page.items])
            page = paginator.next_page(page)

        # go back from the last page all the way to the first
        # rebuild the last page through its cursor, then walk back
        back_pages = []
        last_cursor = paginator.first_page()
        while last_cursor.next_cursor:
            last_cursor = paginator.next_page(last_cursor)
        while last_cursor is not None:
            back_pages.append([r["id"] for r in last_cursor.items])
            last_cursor = paginator.prev_page(last_cursor)
        back_pages.reverse()
        self.assertEqual(back_pages, forward_pages)

    def test_zigzag_forward_backward_is_stable(self):
        rows = [{"id": i, "a": i} for i in range(12)]
        paginator = Paginator(
            make_store(rows), SortSpec([("a", ASC)]), page_size=3, secret=SECRET
        )
        p1 = paginator.first_page()
        p2 = paginator.next_page(p1)
        p3 = paginator.next_page(p2)
        self.assertEqual(paginator.prev_page(p2).items, p1.items)
        self.assertEqual(paginator.prev_page(p3).items, p2.items)
        self.assertEqual(paginator.next_page(paginator.prev_page(p3)).items, p3.items)
        self.assertEqual(paginator.prev_page(paginator.next_page(p1)).items, p1.items)


class MutationInterleavingTest(unittest.TestCase):
    """Deterministic simulation of rows inserted / deleted / updated while
    the client walks the pages one by one."""

    def setUp(self):
        self.store = make_store(
            [{"id": i, "seq": float(i), "status": "old"} for i in range(1, 11)]
        )
        self.paginator = Paginator(
            self.store, SortSpec([("seq", ASC)]), page_size=3, secret=SECRET
        )

    def test_insert_ahead_seen_insert_behind_skipped_deleted_marked(self):
        p1 = self.paginator.first_page()
        self.assertEqual([r["id"] for r in p1.items], [1, 2, 3])

        # after the cursor: id=11 sorts BEHIND the cursor (seq 2.5) -> skipped
        self.store.insert({"id": 11, "seq": 2.5, "status": "new"})
        # ahead of the cursor: id=12 sorts between 5 and 6 -> must appear
        self.store.insert({"id": 12, "seq": 5.5, "status": "new"})
        # unseen row deleted -> disappears and is reported as deleted
        self.store.delete(5)
        # already seen row deleted -> simply gone
        self.store.delete(2)
        # non-key column update on an unseen row -> new data still seen
        self.store.update(7, {"status": "updated"})

        p2 = self.paginator.next_page(p1)
        self.assertEqual([r["id"] for r in p2.items], [4, 12, 6])
        self.assertIn(5, p2.deleted)
        self.assertNotIn(2, p2.deleted)  # already paged past -> not in range

        p3 = self.paginator.next_page(p2)
        self.assertEqual([r["id"] for r in p3.items], [7, 8, 9])
        self.assertEqual(p3.items[0]["status"], "updated")
        self.assertEqual(p3.deleted, [])

        p4 = self.paginator.next_page(p3)
        self.assertEqual([r["id"] for r in p4.items], [10])
        self.assertFalse(p4.has_next)
        self.assertIsNone(p4.next_cursor)

        seen = [1, 2, 3] + [r["id"] for r in p2.items] + [
            r["id"] for r in p3.items
        ] + [r["id"] for r in p4.items]
        self.assertEqual(len(seen), len(set(seen)))  # no duplicates
        self.assertNotIn(11, seen)  # inserted behind cursor never reappears
        self.assertNotIn(5, seen)   # deleted before reached never appears

    def test_delete_entire_page_then_continue(self):
        p1 = self.paginator.first_page()
        for rid in (4, 5, 6):
            self.store.delete(rid)
        p2 = self.paginator.next_page(p1)
        # ids 4-6 vanished; the next window starts at 7 and reports tombstones
        self.assertEqual([r["id"] for r in p2.items], [7, 8, 9])
        self.assertIn(4, p2.deleted)
        self.assertIn(5, p2.deleted)
        self.assertIn(6, p2.deleted)

    def test_resurrecting_deleted_id_with_insert(self):
        self.store.delete(4)
        p1 = self.paginator.first_page()
        p2 = self.paginator.next_page(p1)
        # tombstone range reports id 4
        self.assertIn(4, p2.deleted)
        # re-insert with same id at a new position: treated as a new row
        self.store.insert({"id": 4, "seq": 40.0, "status": "resurrected"})
        rest, _ = all_ids_from(p2, self.paginator)
        self.assertIn(4, rest)
        self.assertEqual(self.store.get(4)["status"], "resurrected")


def all_ids_from(page, paginator):
    seen, deleted = [], []
    while page is not None:
        seen.extend(r["id"] for r in page.items)
        deleted.extend(page.deleted)
        page = paginator.next_page(page)
    return seen, deleted


class DeepPaginationTest(unittest.TestCase):
    def test_walk_20k_rows_without_duplicates_or_gaps(self):
        n = 20_000
        store = make_store(
            [{"id": i, "g": i % 7, "v": i * 3 % 97} for i in range(n)]
        )
        paginator = Paginator(
            store, SortSpec([("g", ASC), ("v", DESC)]), page_size=200,
            secret=SECRET,
        )
        seen, deleted = all_ids(paginator)
        self.assertEqual(len(seen), n)
        self.assertEqual(len(set(seen)), n)  # every row exactly once
        expected = [
            r["id"] for r in sorted(
                store.snapshot(paginator._spec)[1],
                key=lambda r: (r["g"], -r["v"], r["id"]),
            )
        ]
        self.assertEqual(seen, expected)


if __name__ == "__main__":
    unittest.main()
