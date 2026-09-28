import random
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sorted_index import SortedIndex


def make_index(keys, block_capacity=64):
    return SortedIndex.bulk_load(((k, k * 10) for k in sorted(keys)),
                                 block_capacity=block_capacity)


class BasicOpsTest(unittest.TestCase):
    def test_put_get_delete(self):
        idx = SortedIndex(block_capacity=8)
        for k in [5, 1, 9, 3, 7, 3, 5]:
            idx.put(k, k * 2)
        self.assertEqual(len(idx), 5)
        self.assertEqual(idx.get(3), 6)
        self.assertEqual(idx.get(100), None)
        self.assertIn(9, idx)
        idx.delete(9)
        self.assertNotIn(9, idx)
        with self.assertRaises(KeyError):
            idx.delete(9)
        self.assertFalse(idx.discard(9))
        self.assertTrue(idx.discard(7))
        self.assertEqual([k for k, _ in idx.items()], [1, 3, 5])

    def test_sorted_iteration_after_random_inserts(self):
        rng = random.Random(7)
        keys = rng.sample(range(10000), 2000)
        idx = SortedIndex(block_capacity=32)
        for k in keys:
            idx.put(k, k)
        self.assertEqual([k for k, _ in idx.items()], sorted(keys))


class RangeDeleteBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.keys = list(range(0, 1000, 2))  # 500 even keys

    def test_empty_interval_is_noop(self):
        for lo, hi in [(100, 100), (200, 100), (0, 0), (999, 999)]:
            idx = make_index(self.keys)
            before = idx.stats()
            self.assertEqual(idx.delete_range(lo, hi), 0)
            self.assertEqual(idx.stats(), before)
            self.assertEqual([k for k, _ in idx.items()], self.keys)

    def test_interval_out_of_range(self):
        for lo, hi in [(-500, -1), (1000, 2000), (2000, 3000)]:
            idx = make_index(self.keys)
            self.assertEqual(idx.delete_range(lo, hi), 0)
            self.assertEqual([k for k, _ in idx.items()], self.keys)
            self.assertEqual(len(idx), 500)

    def test_interval_covering_everything(self):
        idx = make_index(self.keys)
        self.assertEqual(idx.delete_range(-10, 10_000), 500)
        self.assertEqual(len(idx), 0)
        stats = idx.stats()
        self.assertEqual(stats["capacity"], 0)
        self.assertEqual(stats["blocks"], 0)
        self.assertEqual(list(idx.items()), [])
        # index still usable afterwards
        idx.put(42, 42)
        self.assertEqual(list(idx.items()), [(42, 42)])

    def test_spanning_many_blocks(self):
        idx = make_index(range(10_000), block_capacity=16)  # ~625 blocks
        removed = idx.delete_range(500, 9_500)
        self.assertEqual(removed, 9_000)
        self.assertEqual([k for k, _ in idx.items()],
                         list(range(500)) + list(range(9_500, 10_000)))
        stats = idx.stats()
        self.assertEqual(stats["size"], 1_000)

    def test_adjacent_consecutive_deletes(self):
        idx = make_index(range(300))
        total = 0
        for lo in range(0, 300, 10):
            total += idx.delete_range(lo, lo + 10)
        self.assertEqual(total, 300)
        self.assertEqual(len(idx), 0)
        self.assertEqual(idx.stats()["capacity"], 0)

    def test_adjacent_deletes_equal_single_delete(self):
        a = make_index(range(500))
        b = make_index(range(500))
        a.delete_range(100, 200)
        a.delete_range(200, 300)
        a.delete_range(300, 400)
        b.delete_range(100, 400)
        self.assertEqual(list(a.items()), list(b.items()))
        self.assertEqual(a.stats(), b.stats())

    def test_edges_inside_blocks(self):
        idx = make_index(range(100), block_capacity=16)
        # 17..82 inclusive -> half-open [17, 83); cuts two blocks mid-way
        removed = idx.delete_range(17, 83)
        self.assertEqual(removed, 66)
        expected = list(range(17)) + list(range(83, 100))
        self.assertEqual([k for k, _ in idx.items()], expected)

    def test_delete_on_empty_index(self):
        idx = SortedIndex()
        self.assertEqual(idx.delete_range(0, 100), 0)
        self.assertEqual(len(idx), 0)


class DifferentialTest(unittest.TestCase):
    """Range delete must produce exactly the same remaining keys and
    size as deleting the same keys one by one (randomized A/B)."""

    def test_range_vs_point_deletes(self):
        for trial in range(60):
            rng = random.Random(10_000 + trial)
            keys = rng.sample(range(5_000), rng.randint(0, 600))
            cap = rng.choice([4, 8, 17, 64, 256])
            a = make_index(keys, block_capacity=cap)
            b = make_index(keys, block_capacity=cap)
            lo = rng.randint(-100, 5_100)
            hi = rng.randint(-100, 5_100)
            if lo > hi:
                lo, hi = hi, lo
            removed_range = a.delete_range(lo, hi)
            removed_point = sum(1 for k in keys
                                if lo <= k < hi and b.discard(k))
            self.assertEqual(removed_range, removed_point)
            self.assertEqual(list(a.items()), list(b.items()))
            self.assertEqual(a.stats()["size"], b.stats()["size"])
            # range delete never keeps more dead capacity than point deletes
            self.assertLessEqual(a.stats()["capacity"], b.stats()["capacity"])

    def test_random_mixed_workload_against_dict(self):
        rng = random.Random(99)
        idx = SortedIndex(block_capacity=16)
        ref = {}
        for _ in range(3_000):
            op = rng.random()
            if op < 0.45:
                k = rng.randint(0, 2_000)
                idx.put(k, k)
                ref[k] = k
            elif op < 0.7:
                k = rng.randint(0, 2_000)
                idx.discard(k)
                ref.pop(k, None)
            else:
                lo = rng.randint(0, 2_000)
                hi = lo + rng.randint(0, 300)
                idx.delete_range(lo, hi)
                for k in [k for k in ref if lo <= k < hi]:
                    del ref[k]
            self.assertEqual([k for k, _ in idx.items()], sorted(ref))
            self.assertEqual(len(idx), len(ref))


class ReclamationTest(unittest.TestCase):
    def test_capacity_actually_freed(self):
        idx = make_index(range(20_000), block_capacity=128)
        before = idx.stats()
        self.assertGreaterEqual(before["capacity"], 20_000)
        idx.delete_range(5_000, 15_000)
        after = idx.stats()
        self.assertEqual(after["size"], 10_000)
        # capacity of the deleted middle is gone, not just marked free
        self.assertLessEqual(after["capacity"],
                             before["capacity"] - 10_000 + 2 * 128)

    def test_inserts_reuse_freed_slots_without_growth(self):
        idx = make_index(range(4_000), block_capacity=64)
        # punch scattered holes via point deletes -> free slots stay
        # (every block keeps its first key, so block boundaries are stable)
        doomed = [k for k in range(4_000) if k % 8 in (3, 5)]
        for k in doomed:
            idx.discard(k)
        mid = idx.stats()
        self.assertEqual(mid["size"], 4_000 - len(doomed))
        self.assertGreater(mid["fragmentation"], 0)
        # reinsert into the holes: size grows, capacity stays flat
        for k in doomed:
            idx.put(k, k * 10)
        end = idx.stats()
        self.assertEqual(end["size"], 4_000)
        self.assertEqual(end["capacity"], mid["capacity"])
        self.assertEqual(end["fragmentation"], end["capacity"] - 4_000)


class ConcurrencyTest(unittest.TestCase):
    def test_readers_see_before_or_after_state(self):
        # One range delete, many readers. Every read must observe either
        # the complete before-delete state or the complete after-delete
        # state: key set, length, stats and boundary-key membership all
        # taken from the SAME snapshot must agree. Repeated rounds with a
        # tiny switch interval maximise the chance of catching a torn
        # publish (new blocks paired with stale starts/size).
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            n = 30_000
            lo, hi = 7_500, 22_500
            removed = hi - lo
            before_keys = list(range(n))
            after_keys = [k for k in range(n) if not lo <= k < hi]
            before_set, after_set = set(before_keys), set(after_keys)
            # boundary and far-away keys exercise the old-starts/new-blocks
            # mismatch that used to raise IndexError or lose a live key
            probes = (0, lo - 1, lo, lo + 1, hi - 2, hi - 1, hi,
                      hi + 1, n - 1)

            for _ in range(8):
                idx = make_index(range(n), block_capacity=256)
                errors = []
                stop = threading.Event()
                barrier = threading.Barrier(5)

                def reader():
                    barrier.wait()
                    while not stop.is_set():
                        snap = idx.snapshot()
                        keys = list(snap.keys())
                        # whole key set must be exactly one legal state
                        if keys == before_keys:
                            allowed = before_set
                            expected_size = n
                        elif keys == after_keys:
                            allowed = after_set
                            expected_size = n - removed
                        else:
                            errors.append("reader observed intermediate key set "
                                          "(len=%d)" % len(keys))
                            return
                        # length / stats from the same snapshot agree
                        stats = snap.stats()
                        if (len(snap) != expected_size
                                or stats["size"] != expected_size
                                or stats["blocks"] != len(snap._blocks)
                                or stats["capacity"]
                                != sum(b.capacity for b in snap._blocks)
                                or stats["fragmentation"]
                                != stats["capacity"] - expected_size):
                            errors.append("snapshot length/stats torn: %r"
                                          % (stats,))
                            return
                        # boundary membership from the SAME snapshot must
                        # match the observed key set (no ghost / lost key)
                        for p in probes:
                            if (p in snap) != (p in allowed):
                                errors.append("boundary membership torn for key "
                                              "%d (present=%r)" % (p, p in snap))
                                return
                            want = p * 10 if p in allowed else None
                            if snap.get(p) != want:
                                errors.append("boundary get torn for key %d "
                                              "(got %r)" % (p, snap.get(p)))
                                return
                        # direct index reads must never explode and must
                        # also report a legal state
                        if len(idx) not in (n, n - removed):
                            errors.append("index length torn: %d" % len(idx))
                            return
                        for p in (lo - 1, hi, n - 1):  # live in both states
                            try:
                                present = p in idx
                                value = idx.get(p)
                            except Exception as exc:  # noqa: BLE001
                                errors.append("live-key probe raised %s: %s"
                                              % (type(exc).__name__, exc))
                                return
                            if not present or value != p * 10:
                                errors.append("live key %d lost mid-delete" % p)
                                return

                threads = [threading.Thread(target=reader) for _ in range(4)]
                for t in threads:
                    t.start()
                barrier.wait()
                idx.delete_range(lo, hi)
                stop.set()
                for t in threads:
                    t.join()
                self.assertEqual(errors, [])
                self.assertEqual([k for k, _ in idx.items()], after_keys)
        finally:
            sys.setswitchinterval(old_interval)

    def test_snapshots_consistent_under_repeated_range_deletes(self):
        n = 20_000
        idx = make_index(range(n), block_capacity=128)
        errors = []
        stop = threading.Event()
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)

        try:
            def reader():
                while not stop.is_set():
                    snap = idx.snapshot()
                    keys = list(snap.keys())
                    if keys != sorted(keys) or len(set(keys)) != len(keys):
                        errors.append("snapshot not strictly sorted unique")
                        return
                    if len(keys) != snap.stats()["size"] or len(snap) != len(keys):
                        errors.append("snapshot size inconsistent")
                        return
                    # every surviving key must map to its original value
                    for k, v in snap.items():
                        if v != k * 10:
                            errors.append("key/value mismatch in snapshot")
                            return
                    # membership checks through the same snapshot agree
                    for p in (0, 99, 100, n - 100, n - 1):
                        if (p in snap) != (p in set(keys)):
                            errors.append("membership inconsistent with key set")
                            return

            threads = [threading.Thread(target=reader) for _ in range(4)]
            for t in threads:
                t.start()
            for lo in range(0, n, 100):  # 200 adjacent range deletes
                idx.delete_range(lo, lo + 100)
            stop.set()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(idx), 0)
        finally:
            sys.setswitchinterval(old_interval)


if __name__ == "__main__":
    unittest.main()
