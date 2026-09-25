"""Concurrency scenario: rows are inserted, deleted and updated by worker
threads while the main thread walks the pages.

Invariants asserted:
  1. no record is emitted twice across pages;
  2. every row that was live (initial set or inserted strictly ahead of the
     cursor) and never deleted before the cursor passed it is seen;
  3. ids reported in ``page.deleted`` really were deleted and were never
     emitted as live items;
  4. traversal finishes cleanly once mutators stop.

Inserts use strictly increasing sort keys, so every inserted row is ahead
of every cursor position at insert time and must be reached eventually.
"""

import random
import threading
import time
import unittest

from cursor_pagination import (
    ASC,
    FORWARD,
    Cursor,
    Paginator,
    SortSpec,
    Store,
    encode_cursor,
)

SECRET = "concurrency-secret"


class ConcurrentMutationTest(unittest.TestCase):
    def test_insert_delete_update_while_paging(self):
        initial_n = 2000
        inserted_n = 800
        delete_ops_per_worker = 300
        n_deleters = 2
        page_size = 50

        store = Store(tombstone_capacity=5000)
        for i in range(1, initial_n + 1):
            store.insert({"id": i, "seq": float(i), "status": "init"})
        seq_lock = threading.Lock()
        seq_counter = [initial_n]
        next_id = [initial_n]
        state_lock = threading.Lock()
        inserted_ids = set()
        deleted_ids = set()
        stop = threading.Event()

        def insert_worker():
            for _ in range(inserted_n):
                with seq_lock:
                    seq_counter[0] += 1
                    next_id[0] += 1
                    seq = seq_counter[0]
                    rid = next_id[0]
                store.insert({"id": rid, "seq": float(seq), "status": "new"})
                with state_lock:
                    inserted_ids.add(rid)
                time.sleep(random.random() * 0.0005)

        def delete_worker(seed):
            rng = random.Random(seed)
            for _ in range(delete_ops_per_worker):
                ids = store.live_ids()
                if not ids:
                    time.sleep(0.001)
                    continue
                rid = rng.choice(ids)
                if store.delete(rid):
                    with state_lock:
                        deleted_ids.add(rid)
                time.sleep(rng.random() * 0.0005)

        def update_worker():
            rng = random.Random(123)
            for _ in range(400):
                ids = store.live_ids()
                if ids:
                    rid = rng.choice(ids)
                    try:
                        store.update(rid, {"status": "touched"})
                    except KeyError:
                        pass
                time.sleep(rng.random() * 0.0005)

        workers = [
            threading.Thread(target=insert_worker, name="inserter"),
            threading.Thread(target=update_worker, name="updater"),
        ]
        for seed in range(n_deleters):
            workers.append(
                threading.Thread(target=delete_worker, args=(seed,), name="deleter")
            )

        spec = SortSpec([("seq", ASC)])
        paginator = Paginator(store, spec, page_size=page_size, secret=SECRET)

        for w in workers:
            w.start()

        seen = []
        reported_deleted = []
        token = None
        iterations = 0
        max_iterations = 100_000
        while True:
            iterations += 1
            self.assertLess(iterations, max_iterations, "paging did not converge")
            page = paginator.page(token)
            seen.extend(r["id"] for r in page.items)
            reported_deleted.extend(page.deleted)

            if page.items:
                # advance the position even when no next page exists yet
                # (more rows may be inserted ahead while workers run)
                token = page.next_cursor or encode_cursor(
                    Cursor(spec.key_values(page.items[-1]), FORWARD), spec, SECRET
                )

            if page.has_next:
                continue
            if any(w.is_alive() for w in workers):
                time.sleep(0.002)
                continue
            break

        for w in workers:
            w.join(timeout=5)
            self.assertFalse(w.is_alive())

        initial_ids = set(range(1, initial_n + 1))
        with state_lock:
            expected = initial_ids | inserted_ids
            deleted = set(deleted_ids)

        # 1. no duplicates
        self.assertEqual(len(seen), len(set(seen)), "a record was emitted twice")
        seen_set = set(seen)

        # 2. every row either was reached live or was deleted before reached
        missing = expected - seen_set - deleted
        self.assertFalse(
            missing,
            "%d live rows were skipped: %s"
            % (len(missing), sorted(missing)[:10]),
        )

        # 3. deleted reports are truthful and never overlap live items
        self.assertTrue(set(reported_deleted).issubset(deleted))
        self.assertTrue(set(reported_deleted).isdisjoint(seen_set))

        # 4. the listing is now drained and stable
        final = paginator.page(token)
        self.assertFalse(final.has_next)
        self.assertEqual(final.items, [])


if __name__ == "__main__":
    unittest.main()
