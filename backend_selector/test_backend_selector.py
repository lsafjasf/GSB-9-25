"""Self-tests for backend_selector. Run: python3 -m unittest -v"""

import math
import threading
import time
import unittest

from backend_selector import (
    LeastConnections,
    NoAvailableBackendError,
    WeightedRoundRobin,
)


def run_selects(sel, n):
    return [sel.select() for _ in range(n)]


class WeightedRoundRobinTest(unittest.TestCase):
    def test_single_node(self):
        s = WeightedRoundRobin()
        s.add("a", 5)
        self.assertEqual(run_selects(s, 7), ["a"] * 7)

    def test_weight_zero_never_selected(self):
        s = WeightedRoundRobin()
        s.add("a", 1)
        s.add("b", 0)
        self.assertEqual(set(run_selects(s, 100)), {"a"})

    def test_all_drained_raises(self):
        s = WeightedRoundRobin()
        s.add("a", 1)
        s.add("b", 2)
        s.set_alive("a", False)
        s.set_alive("b", False)
        with self.assertRaises(NoAvailableBackendError):
            s.select()
        # also: all weights zero
        s.set_alive("a", True)
        s.set_weight("a", 0)
        s.set_weight("b", 0)
        with self.assertRaises(NoAvailableBackendError):
            s.select()

    def test_drained_node_not_selected_and_recovers_within_bound(self):
        s = WeightedRoundRobin()
        s.add("a", 1)
        s.add("b", 2)
        s.add("c", 3)
        s.set_alive("c", False)
        picks = run_selects(s, 60)
        self.assertNotIn("c", picks)
        # restore: total W = 6, w_c = 3 -> bound = ceil(6/3) = 2
        s.set_alive("c", True)
        bound = math.ceil(6 / 3)
        picks = run_selects(s, bound)
        self.assertIn("c", picks)

    def test_recovery_bound_extreme_weight(self):
        # w=1 among total W=10001 -> must reappear within 10001 picks
        s = WeightedRoundRobin()
        s.add("big", 10000)
        s.add("small", 1)
        s.set_alive("small", False)
        run_selects(s, 50)
        s.set_alive("small", True)
        bound = math.ceil(10001 / 1)
        picks = run_selects(s, bound)
        self.assertIn("small", picks)
        first = picks.index("small")
        self.assertLessEqual(first + 1, bound)

    def test_distribution_matches_weights(self):
        weights = {"a": 5, "b": 3, "c": 2}
        s = WeightedRoundRobin()
        for nid, w in weights.items():
            s.add(nid, w)
        n = 100000
        run_selects(s, n)
        total_w = sum(weights.values())
        stats = s.stats()
        max_dev = 0.0
        for nid, w in weights.items():
            expected = w / total_w
            _, share = stats[nid]
            dev = abs(share - expected)
            max_dev = max(max_dev, dev)
            print(f"  node={nid} weight={w} expected={expected:.4f} "
                  f"actual={share:.4f} dev={dev:.6f}")
        # SWRR is exact over each period of sum(weights); residual dev <= 1/n * W
        self.assertLess(max_dev, 0.001)

    def test_extreme_weight_ratio(self):
        s = WeightedRoundRobin()
        s.add("heavy", 10000)
        s.add("light", 1)
        n = 10001 * 3  # whole periods -> exact
        run_selects(s, n)
        stats = s.stats()
        self.assertEqual(stats["heavy"][0], 10000 * 3)
        self.assertEqual(stats["light"][0], 3)

    def test_determinism_same_sequence_same_result(self):
        def build_and_run():
            s = WeightedRoundRobin()
            s.add("a", 3)
            s.add("b", 1)
            s.add("c", 2)
            out = run_selects(s, 20)
            s.set_alive("b", False)
            out += run_selects(s, 20)
            s.set_weight("a", 5)
            s.set_alive("b", True)
            out += run_selects(s, 20)
            s.remove("c")
            out += run_selects(s, 20)
            return out

        r1, r2 = build_and_run(), build_and_run()
        self.assertEqual(r1, r2)

    def test_tie_break_smallest_id(self):
        s = WeightedRoundRobin()
        s.add("b", 1)
        s.add("a", 1)
        # equal weights: first pick must be the smallest id "a"
        self.assertEqual(s.select(), "a")
        self.assertEqual(s.select(), "b")

    def test_dynamic_membership_change(self):
        s = WeightedRoundRobin()
        s.add("a", 1)
        s.add("b", 1)
        run_selects(s, 10)
        s.add("c", 2)
        s.set_weight("a", 4)
        s.remove("b")
        picks = run_selects(s, 60)
        self.assertNotIn("b", picks)
        stats = s.stats()
        # after change: a:c = 4:2 = 2:1 over the last 60 picks
        self.assertEqual(stats["a"][0] - 5, 40)
        self.assertEqual(stats["c"][0], 20)

    def test_concurrent_selection_thread_safe(self):
        s = WeightedRoundRobin()
        for i in range(4):
            s.add(f"n{i}", i + 1)
        n_threads, per_thread = 8, 5000
        errors = []

        def worker():
            try:
                run_selects(s, per_thread)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        # no selection lost or double-counted under concurrency
        self.assertEqual(s.total_selected(), n_threads * per_thread)
        # distribution still proportional
        stats = s.stats()
        total = n_threads * per_thread
        for i in range(4):
            expected = (i + 1) / 10
            self.assertAlmostEqual(stats[f"n{i}"][1], expected, delta=0.01)


class LeastConnectionsTest(unittest.TestCase):
    def test_single_node(self):
        s = LeastConnections()
        s.add("a", 3)
        self.assertEqual(run_selects(s, 5), ["a"] * 5)

    def test_all_drained_raises(self):
        s = LeastConnections()
        s.add("a", 1)
        s.set_alive("a", False)
        with self.assertRaises(NoAvailableBackendError):
            s.select()

    def test_weight_zero_never_selected(self):
        s = LeastConnections()
        s.add("a", 1)
        s.add("b", 0)
        self.assertEqual(set(run_selects(s, 20)), {"a"})

    def test_reflects_in_flight_changes(self):
        s = LeastConnections()
        s.add("a", 1)
        s.add("b", 1)
        first = s.select()          # a (tie -> smallest id), a.in_flight=1
        self.assertEqual(first, "a")
        second = s.select()         # b now has fewer in-flight
        self.assertEqual(second, "b")
        s.release("a")
        third = s.select()          # a free again
        self.assertEqual(third, "a")
        self.assertEqual(s.in_flight(), {"a": 1, "b": 1})

    def test_weighted_least_connections(self):
        s = LeastConnections()
        s.add("strong", 4)
        s.add("weak", 1)
        # hold all connections open: ratio in_flight/weight decides
        picks = run_selects(s, 10)
        self.assertEqual(picks.count("strong"), 8)
        self.assertEqual(picks.count("weak"), 2)
        self.assertEqual(s.in_flight("strong"), 8)
        self.assertEqual(s.in_flight("weak"), 2)

    def test_drained_node_not_selected_and_recovers(self):
        s = LeastConnections()
        s.add("a", 1)
        s.add("b", 1)
        s.set_alive("b", False)
        picks = run_selects(s, 10)
        self.assertNotIn("b", picks)
        s.set_alive("b", True)
        # b has 0 in-flight vs a's 10 -> next pick is immediately b
        self.assertEqual(s.select(), "b")

    def test_determinism(self):
        def build_and_run():
            s = LeastConnections()
            s.add("a", 2)
            s.add("b", 1)
            s.add("c", 3)
            out = []
            for _ in range(30):
                nid = s.select()
                out.append(nid)
                if len(out) % 3 == 0:
                    s.release(nid)
            s.set_alive("c", False)
            out += run_selects(s, 10)
            return out

        self.assertEqual(build_and_run(), build_and_run())

    def test_context_manager_releases(self):
        s = LeastConnections()
        s.add("a", 1)
        with s.connection() as nid:
            self.assertEqual(nid, "a")
            self.assertEqual(s.in_flight("a"), 1)
        self.assertEqual(s.in_flight("a"), 0)

    def test_concurrent_select_release(self):
        s = LeastConnections()
        for i in range(4):
            s.add(f"n{i}", 1)
        n_threads, per_thread = 8, 2000
        errors = []

        def worker():
            try:
                for _ in range(per_thread):
                    with s.connection():
                        # hold briefly so in-flight connections genuinely
                        # overlap across threads
                        time.sleep(0.0002)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertEqual(s.total_selected(), n_threads * per_thread)
        # everything released
        self.assertEqual(sum(s.in_flight().values()), 0)
        # equal weights + overlapping load -> spread across all nodes
        stats = s.stats()
        for i in range(4):
            self.assertGreater(stats[f"n{i}"][1], 0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
