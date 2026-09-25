"""Regression tests for the fixed snapshot-semantics MemoryIndex."""

import gc
import sys
import threading
import tracemalloc
import unittest
import weakref

import conformance as c
from memory_index import (
    MemoryIndex,
    SnapshotIterator,
    IteratorClosedError,
)

fixed = c.FACTORIES["fixed"]


class _Value:
    """Plain user value that supports weak references."""

    __slots__ = ("n", "__weakref__")

    def __init__(self, n=0):
        self.n = n


class SnapshotTraversalConformance(unittest.TestCase):
    """The four reported bug scenarios must now have zero violations."""

    def test_delete_current(self):
        self.assertEqual(c.scenario_delete_current(fixed), [])

    def test_delete_unvisited(self):
        self.assertEqual(c.scenario_delete_unvisited(fixed), [])

    def test_delete_then_reinsert_same_key(self):
        self.assertEqual(c.scenario_delete_and_reinsert(fixed), [])

    def test_compact_during_iteration(self):
        self.assertEqual(c.scenario_compact_during_iteration(fixed), [])

    def test_concurrent_interleaving(self):
        self.assertEqual(c.scenario_concurrent_interleaving(fixed), [])

    def test_stats_consistency(self):
        self.assertEqual(c.scenario_stats_consistency(fixed), [])


class SnapshotSemantics(unittest.TestCase):
    def test_view_is_frozen_at_iterator_creation(self):
        index = MemoryIndex()
        index.insert("a", 1)
        index.insert("b", 2)
        iterator = index.items()
        index.insert("c", 3)        # after snapshot -> invisible
        index.delete("a")           # after snapshot -> still in snapshot
        index.insert("b", 99)       # overwrite -> snapshot keeps old value
        pairs = dict(iterator)
        self.assertEqual(pairs, {"a": 1, "b": 2})
        self.assertEqual(dict(index.items()), {"b": 99, "c": 3})

    def test_every_snapshot_record_visited_exactly_once(self):
        for size in (1, 7, 50, 500):
            index = MemoryIndex()
            for key in range(size):
                index.insert(key, key)
            # Snapshot is frozen here; later deletes must not shrink it and
            # later inserts must not extend it.
            expected = set(range(size))
            iterator = index.items()
            index.compact()
            for key in range(0, size, 3):
                index.delete(key)
            for key in range(size, size + size):
                index.insert(key, key)
            visited = [key for key, _ in iterator]
            self.assertEqual(len(visited), len(expected))
            self.assertEqual(set(visited), expected)
            self.assertEqual(len(visited), len(set(visited)))

    def test_keys_and_values_match_items_snapshot(self):
        index = MemoryIndex()
        for key in range(10):
            index.insert(key, key * 2)
        self.assertEqual(list(index.keys()), [k for k, _ in index.items()])
        self.assertEqual(list(index.values()), [v for _, v in index.items()])

    def test_old_iterator_keeps_working_after_compact(self):
        index = MemoryIndex()
        for key in range(20):
            index.insert(key, "v" + str(key))
        iterator = index.items()
        first = next(iterator)
        index.compact()
        index.compact()  # repeated rebuilds must not invalidate it
        rest = list(iterator)
        keys = [first[0]] + [k for k, _ in rest]
        self.assertEqual(sorted(keys), list(range(20)))

    def test_closed_iterator_raises_clear_error(self):
        index = MemoryIndex()
        index.insert(1, "x")
        iterator = index.items()
        iterator.close()
        with self.assertRaises(IteratorClosedError):
            next(iterator)

    def test_context_manager_closes_snapshot(self):
        index = MemoryIndex()
        index.insert(1, "x")
        with index.items() as iterator:
            self.assertEqual(list(iterator), [(1, "x")])
        with self.assertRaises(IteratorClosedError):
            next(iterator)


class ResourceRelease(unittest.TestCase):
    def test_snapshot_buffer_released_after_exhaustion(self):
        index = MemoryIndex()
        value = _Value()
        index.insert("k", value)
        value_ref = weakref.ref(value)
        iterator = index.items()
        self.assertEqual(index.active_iterators(), 1)
        list(iterator)  # exhaust
        self.assertEqual(index.active_iterators(), 0)
        # Delete from the live table too, so the snapshot is the only owner.
        index.delete("k")
        del value
        gc.collect()
        self.assertIsNone(
            value_ref(),
            "value still alive after iterator exhaustion",
        )

    def test_snapshot_buffer_released_on_early_close(self):
        index = MemoryIndex()
        values = [_Value(key) for key in range(100)]
        for key, value in enumerate(values):
            index.insert(key, value)
        del key, value
        iterator = index.items()
        first_pair = next(iterator)
        refs = [weakref.ref(held) for held in values]
        del first_pair
        del values
        for key in range(100):
            index.delete(key)
        index.compact()
        gc.collect()
        # Live table released everything; iterator still open pins snapshot.
        self.assertEqual(index.active_iterators(), 1)
        alive_during = sum(1 for r in refs if r() is not None)
        self.assertEqual(alive_during, 100)
        iterator.close()
        del iterator
        gc.collect()
        self.assertEqual(index.active_iterators(), 0)
        alive_after = sum(1 for r in refs if r() is not None)
        self.assertEqual(
            alive_after, 0, "snapshot values survived close()"
        )

    def test_unclosed_abandoned_iterator_collected(self):
        index = MemoryIndex()
        index.insert(1, 1)
        iterator = index.items()
        ref = weakref.ref(iterator)
        del iterator
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(index.active_iterators(), 0)

    def test_long_running_repeated_traversals_do_not_grow(self):
        tracemalloc.start()
        try:
            index = MemoryIndex(2048)
            for key in range(1000):
                index.insert(key, "value-%d" % key)

            def total_traced_bytes():
                current, _peak = tracemalloc.get_traced_memory()
                return current

            for _ in range(50):
                count = 0
                with index.items() as iterator:
                    for _key, _value in iterator:
                        count += 1
                self.assertEqual(count, 1000)
                self.assertEqual(index.active_iterators(), 0)
            gc.collect()
            baseline = total_traced_bytes()
            for _ in range(200):
                count = 0
                iterator = index.items()
                for _key, _value in iterator:
                    count += 1
                self.assertEqual(count, 1000)
            gc.collect()
            after = total_traced_bytes()
            drift = after - baseline
            # Allow small allocator noise, but no per-iteration retention.
            self.assertLess(
                drift,
                512 * 1024,
                "memory grew by %d bytes across 200 long traversals" % drift,
            )
        finally:
            tracemalloc.stop()

    def test_mutation_inside_traversal_releases_snapshot_on_exhaustion(self):
        index = MemoryIndex(check_invariants=True)
        for key in range(300):
            index.insert(key, object())
        iterator = index.items()
        for step, (key, value) in enumerate(iterator):
            if step % 2 == 0:
                index.delete(key)
            if step % 7 == 0:
                index.insert(10_000 + step, _Value(step))
            if step % 11 == 0:
                index.compact()
        self.assertEqual(index.active_iterators(), 0)


class InvariantsAndStats(unittest.TestCase):
    def test_invariants_hold_through_randomized_operations(self):
        import random
        rng = random.Random(1234)
        index = MemoryIndex(8, check_invariants=True)
        mirror = set()
        for step in range(5000):
            key = rng.randrange(0, 120)
            action = rng.random()
            if action < 0.55:
                index.insert(key, step)
                mirror.add(key)
            elif action < 0.85:
                self.assertEqual(index.delete(key), key in mirror)
                mirror.discard(key)
            elif action < 0.95:
                if rng.random() < 0.5:
                    index.compact()
            else:
                index.clear()
                mirror.clear()
            if step % 137 == 0:
                stats = index.check_invariants()
                self.assertEqual(stats["entries"], len(mirror))
                self.assertLessEqual(stats["entries"], stats["capacity"])
        stats = index.check_invariants()
        self.assertEqual(stats["entries"], len(mirror))
        self.assertEqual(set(index.keys()), mirror)

    def test_capacity_shapes_and_counters(self):
        index = MemoryIndex(10)
        self.assertEqual(index.capacity, 16)
        for key in range(12):
            index.insert(key, key)
        self.assertGreaterEqual(index.capacity, 16)
        self.assertEqual(index.stats()["deleted"], 0)
        for key in range(6):
            index.delete(key)
        self.assertEqual(index.stats()["deleted"] + len(index),
                         index.stats()["deleted"] + 6)
        self.assertEqual(len(index), 6)
        index.compact()
        stats = index.stats()
        self.assertEqual(stats["deleted"], 0)
        self.assertEqual(stats["entries"], 6)
        self.assertEqual(index.active_iterators(), 0)


class ThreadedInterleaving(unittest.TestCase):
    def test_writer_threads_while_reader_uses_snapshot(self):
        index = MemoryIndex(256, check_invariants=True)
        for key in range(500):
            index.insert(key, 0)
        errors = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    iterator = index.items()
                    seen = set()
                    pairs = []
                    for key, value in iterator:
                        if key in seen:
                            raise AssertionError("duplicate key %r" % (key,))
                        seen.add(key)
                        pairs.append((key, value))
                    # Snapshot size must be one self-consistent table size.
                    self.assertEqual(len(pairs), len(seen))
                    iterator.close()
            except Exception as exc:  # pragma: no cover - failure reporter
                errors.append(exc)

        def writer(seed):
            rng = threading.local()
            import random
            r = random.Random(seed)
            while not stop.is_set():
                key = r.randrange(0, 1000)
                if r.random() < 0.5:
                    index.insert(key, r.randrange(0, 10))
                else:
                    index.delete(key)

        readers = [threading.Thread(target=reader) for _ in range(2)]
        writers = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        for thread in readers + writers:
            thread.start()
        # Let it run briefly, then signal stop and join.
        stop.wait(2.0)
        stop.set()
        for thread in readers + writers:
            thread.join(timeout=5)
        for thread in readers + writers:
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        index.check_invariants()
        self.assertEqual(index.active_iterators(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
