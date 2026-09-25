"""Self tests for consistent_hash (stdlib unittest only).

Run:  python3 test_consistent_hash.py -v
"""

import random
import threading
import unittest

from consistent_hash import ConsistentHash


def build(node_weights, base_vnodes=160):
    ch = ConsistentHash(base_vnodes=base_vnodes)
    for name, w in node_weights:
        ch.add_node(name, weight=w)
    return ch


def distribution(ch, keys):
    counts = {n: 0 for n in ch.nodes}
    for k in keys:
        counts[ch.get_node(k)] += 1
    return counts


class MappingTest(unittest.TestCase):
    def test_empty_ring_returns_none(self):
        ch = ConsistentHash()
        self.assertIsNone(ch.get_node("anything"))

    def test_single_node_gets_everything(self):
        ch = build([("solo", 1.0)])
        for i in range(1000):
            self.assertEqual(ch.get_node(f"key-{i}"), "solo")

    def test_remove_all_nodes(self):
        ch = build([("a", 1.0), ("b", 1.0)])
        ch.remove_node("a")
        ch.remove_node("b")
        self.assertEqual(len(ch), 0)
        self.assertIsNone(ch.get_node("key"))
        self.assertEqual(ch.ring_size, 0)

    def test_empty_key(self):
        ch = build([("a", 1.0), ("b", 1.0)])
        node = ch.get_node("")
        self.assertIn(node, {"a", "b"})
        self.assertEqual(ch.get_node(""), node)  # deterministic

    def test_key_space_much_larger_than_ring(self):
        # 200k keys against a tiny ring (10 vnodes/node * 3 nodes = 30 points)
        ch = build([("a", 1.0), ("b", 1.0), ("c", 1.0)], base_vnodes=10)
        keys = [f"big-key-space-{i}" for i in range(200_000)]
        counts = distribution(ch, keys)
        self.assertEqual(sum(counts.values()), len(keys))
        for n, c in counts.items():
            self.assertGreater(c, 0, f"node {n} got no keys")

    def test_mapping_is_deterministic(self):
        ch = build([("a", 1.0), ("b", 2.0), ("c", 1.0)])
        for i in range(500):
            k = f"det-{i}"
            self.assertEqual(ch.get_node(k), ch.get_node(k))

    def test_mapping_independent_of_insertion_order(self):
        nodes = [("n1", 1.0), ("n2", 2.0), ("n3", 0.5), ("n4", 1.5), ("n5", 1.0)]
        forward = build(nodes)
        backward = build(list(reversed(nodes)))
        shuffled = build(random.Random(42).sample(nodes, len(nodes)))
        for i in range(5000):
            k = f"order-{i}"
            a, b, c = forward.get_node(k), backward.get_node(k), shuffled.get_node(k)
            self.assertEqual(a, b)
            self.assertEqual(a, c)

    def test_weight_affects_share(self):
        ch = build([("light", 1.0), ("heavy", 3.0)], base_vnodes=400)
        keys = [f"w-{i}" for i in range(100_000)]
        counts = distribution(ch, keys)
        ratio = counts["heavy"] / counts["light"]
        self.assertGreater(ratio, 2.4)   # ideal 3.0
        self.assertLess(ratio, 3.6)

    def test_get_nodes_returns_distinct_replicas(self):
        ch = build([("a", 1.0), ("b", 1.0), ("c", 1.0)])
        replicas = ch.get_nodes("some-key", 2)
        self.assertEqual(len(replicas), 2)
        self.assertNotEqual(replicas[0], replicas[1])
        self.assertEqual(replicas[0], ch.get_node("some-key"))

    def test_invalid_operations(self):
        ch = ConsistentHash()
        with self.assertRaises(ValueError):
            ch.add_node("x", weight=0)
        with self.assertRaises(KeyError):
            ch.remove_node("missing")
        ch.add_node("x")
        with self.assertRaises(ValueError):
            ch.set_weight("x", -1)


class MigrationTest(unittest.TestCase):
    KEYS = [f"mig-{i}" for i in range(100_000)]

    def _mapping(self, ch):
        return {k: ch.get_node(k) for k in self.KEYS}

    def test_add_node_moves_only_to_new_node(self):
        ch = build([(f"n{i}", 1.0) for i in range(5)])
        before = self._mapping(ch)
        ch.add_node("new")
        after = self._mapping(ch)
        moved = [k for k in self.KEYS if before[k] != after[k]]
        # no key may move between two old nodes
        for k in moved:
            self.assertEqual(after[k], "new")
        ratio = len(moved) / len(self.KEYS)
        self.assertGreater(ratio, 1 / 6 * 0.6)   # theory: 1/(N+1) = 16.7%
        self.assertLess(ratio, 1 / 6 * 1.4)

    def test_remove_node_moves_only_from_removed(self):
        ch = build([(f"n{i}", 1.0) for i in range(6)])
        before = self._mapping(ch)
        ch.remove_node("n5")
        after = self._mapping(ch)
        moved = [k for k in self.KEYS if before[k] != after[k]]
        for k in moved:
            self.assertEqual(before[k], "n5")  # only its keys migrate
        ratio = len(moved) / len(self.KEYS)
        self.assertGreater(ratio, 1 / 6 * 0.6)   # theory: 1/N = 16.7%
        self.assertLess(ratio, 1 / 6 * 1.4)

    def test_weight_change_migration_proportional(self):
        ch = build([(f"n{i}", 1.0) for i in range(4)], base_vnodes=400)
        before = self._mapping(ch)
        ch.set_weight("n0", 2.0)  # share 1/4 -> 2/5, delta = 15%
        after = self._mapping(ch)
        moved = [k for k in self.KEYS if before[k] != after[k]]
        for k in moved:  # keys only flow toward the grown node
            self.assertEqual(after[k], "n0")
        ratio = len(moved) / len(self.KEYS)
        self.assertGreater(ratio, 0.15 * 0.7)
        self.assertLess(ratio, 0.15 * 1.3)


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_reads_consistent(self):
        ch = build([(f"n{i}", 1.0) for i in range(8)])
        keys = [f"cc-{i}" for i in range(20_000)]
        expected = {k: ch.get_node(k) for k in keys}
        errors = []

        def reader():
            try:
                for k in keys:
                    if ch.get_node(k) != expected[k]:
                        errors.append(k)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_reads_during_mutation_stay_valid(self):
        ch = build([(f"n{i}", 1.0) for i in range(4)])
        all_nodes = {f"n{i}" for i in range(40)}
        stop = threading.Event()
        failures = []

        def reader():
            rng = random.Random(threading.get_ident())
            while not stop.is_set():
                node = ch.get_node(f"rw-{rng.randrange(10_000)}")
                if node is not None and node not in all_nodes:
                    failures.append(node)

        readers = [threading.Thread(target=reader) for _ in range(8)]
        for t in readers:
            t.start()
        for i in range(4, 40):
            ch.add_node(f"n{i}")
            if i % 3 == 0:
                ch.remove_node(f"n{i - 1}")
                ch.add_node(f"n{i - 1}", weight=2.0)
        stop.set()
        for t in readers:
            t.join()
        self.assertEqual(failures, [])
        # ring still works correctly after the churn
        self.assertIsNotNone(ch.get_node("final-check"))


if __name__ == "__main__":
    unittest.main()
