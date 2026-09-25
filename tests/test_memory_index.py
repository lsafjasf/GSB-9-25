"""Regression, invariant and memory tests for the fixed MemoryIndex.

Semantics under test: SNAPSHOT. An iterator sees exactly the records that
were active when it was created, each exactly once.
"""

import gc
import random
import tracemalloc
import unittest

from memory_index import MemoryIndex


def _filled(keys):
    idx = MemoryIndex()
    for k in keys:
        idx.put(k, str(k).upper())
    return idx


class RegressionTests(unittest.TestCase):
    """The four production scenarios, now expecting correct behaviour."""

    def test_delete_current_does_not_skip_next(self):
        idx = _filled(["a", "b", "c", "d"])
        it = iter(idx)
        first, _ = next(it)
        idx.delete(first)
        rest = [k for k, _ in it]
        self.assertEqual(rest, ["b", "c", "d"])  # nothing skipped

    def test_delete_unvisited_during_iteration(self):
        idx = _filled(["a", "b", "c", "d"])
        it = iter(idx)
        next(it)  # consume 'a'
        idx.delete("c")  # 'c' not yet visited
        rest = [k for k, _ in it]
        # Snapshot semantics: 'c' was active at iterator creation.
        self.assertEqual(rest, ["b", "c", "d"])

    def test_delete_then_reinsert_same_key_visits_once(self):
        idx = _filled(["a", "b", "c"])
        it = iter(idx)
        first, _ = next(it)
        idx.delete(first)
        idx.put(first, "A2")
        visited = [first] + [k for k, _ in it]
        self.assertEqual(visited.count("a"), 1)
        self.assertEqual(sorted(visited), ["a", "b", "c"])

    def test_interleaved_insert_delete_during_iteration(self):
        keys = [f"k{i}" for i in range(50)]
        idx = _filled(keys)
        visited = []
        for k, _ in idx:
            visited.append(k)
            n = int(k[1:])
            if n % 5 == 0 and n + 10 < 50:
                idx.delete(f"k{n + 10}")  # delete an unvisited record
            if n % 7 == 0:
                idx.put(f"new{n}", "X")   # insert during traversal
        # Snapshot: exactly the original 50 keys, each exactly once.
        self.assertEqual(sorted(visited), sorted(keys))
        self.assertEqual(len(visited), len(set(visited)))

    def test_old_iterator_keeps_working_after_cleanup(self):
        idx = _filled(["a", "b", "c", "d"])
        it = iter(idx)
        next(it)  # consume 'a'
        idx.delete("c")
        idx.cleanup()  # must not invalidate the live iterator
        rest = [k for k, _ in it]
        self.assertEqual(rest, ["b", "c", "d"])  # per snapshot semantics
        self.assertNotIn("c", idx)               # but gone from the index

    def test_new_iterator_after_cleanup_sees_reality(self):
        idx = _filled(["a", "b", "c", "d"])
        idx.delete("c")
        idx.cleanup()
        self.assertEqual(sorted(k for k, _ in idx), ["a", "b", "d"])


class InvariantTests(unittest.TestCase):
    def test_stats_consistent_at_every_step(self):
        rng = random.Random(20260925)
        idx = MemoryIndex()
        model = {}
        for _ in range(20_000):
            op = rng.random()
            if op < 0.45:
                key = rng.randrange(300)
                idx.put(key, key * 10)
                model[key] = key * 10
            elif op < 0.75 and model:
                key = rng.choice(tuple(model))
                idx.delete(key)
                del model[key]
            elif op < 0.85:
                idx.cleanup()
            elif op < 0.95 and model:
                key = rng.choice(tuple(model))
                self.assertEqual(idx.get(key), model[key])
            else:
                # traverse with concurrent mutation, then verify snapshot
                baseline = dict(model)
                seen = [k for k, _ in idx]
                self.assertEqual(len(seen), len(set(seen)))  # no duplicates
                self.assertEqual(sorted(seen), sorted(baseline))
            idx.check_invariants()
            self.assertEqual(len(idx), len(model))
            st = idx.stats()
            self.assertEqual(st["size"], len(model))
            self.assertEqual(st["capacity"], st["size"] + st["deleted"])
            self.assertGreaterEqual(st["deleted"], 0)

    def test_delete_missing_key_raises_and_keeps_stats(self):
        idx = _filled(["a"])
        before = idx.stats()
        with self.assertRaises(KeyError):
            idx.delete("nope")
        self.assertEqual(idx.stats(), before)
        idx.check_invariants()


class ResourceTests(unittest.TestCase):
    def test_snapshot_released_on_exhaustion(self):
        idx = _filled(range(1000))
        self.assertEqual(idx.active_iterators, 0)
        for _ in idx:
            pass
        self.assertEqual(idx.active_iterators, 0)

    def test_close_releases_snapshot(self):
        idx = _filled(range(1000))
        it = iter(idx)
        next(it)
        self.assertEqual(idx.active_iterators, 1)
        it.close()
        self.assertEqual(idx.active_iterators, 0)
        with self.assertRaises(StopIteration):
            next(it)

    def test_context_manager_releases_snapshot(self):
        idx = _filled(range(1000))
        with iter(idx) as it:
            next(it)
            self.assertEqual(idx.active_iterators, 1)
        self.assertEqual(idx.active_iterators, 0)

    def test_capacity_bounded_under_churn(self):
        idx = MemoryIndex()
        for round_no in range(200):
            for i in range(100):
                idx.put((round_no, i), i)
            for i in range(100):
                idx.delete((round_no, i))
            if round_no % 10 == 0:
                idx.cleanup()
        idx.cleanup()
        st = idx.stats()
        self.assertEqual(st["size"], 0)
        self.assertEqual(st["deleted"], 0)
        self.assertEqual(st["capacity"], 0)
        idx.check_invariants()

    def test_long_traversal_memory_stable(self):
        """Many full traversals with concurrent churn: memory must not grow."""
        n = 50_000
        idx = MemoryIndex()
        for i in range(n):
            idx.put(i, i)
        gc.collect()
        tracemalloc.start()
        try:
            for round_no in range(30):
                count = 0
                for k, v in idx:
                    count += 1
                    if k % 3 == 0:
                        idx.delete(k)
                        idx.put(k, v)  # churn: slot reuse keeps capacity flat
                self.assertEqual(count, n)               # no skip/dup
                self.assertEqual(idx.active_iterators, 0)  # buffers released
                if round_no % 5 == 0:
                    idx.cleanup()
                idx.check_invariants()
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        st = idx.stats()
        self.assertLessEqual(st["capacity"], n)  # no slot leakage
        # One snapshot of n small tuples is a few MB; anything far above
        # that means buffers accumulate across iterations.
        self.assertLess(peak, 32 * 1024 * 1024)
        print(f"\n[memory] traversals=30 x {n} items, "
              f"tracemalloc peak={peak / 1024 / 1024:.2f} MiB, "
              f"end capacity={st['capacity']}, size={st['size']}, "
              f"active_iterators={idx.active_iterators}")


if __name__ == "__main__":
    unittest.main()
