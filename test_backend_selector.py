"""Self-tests for backend_selector (stdlib unittest only)."""

import math
import threading
import unittest

from backend_selector import (
    LeastConnections,
    NoBackendAvailableError,
    SmoothWeightedRoundRobin,
)


def run_script(selector, script):
    """Replay a deterministic op script; return list of selected ids."""
    out = []
    for op, *args in script:
        if op == "select":
            out.append(selector.select())
        elif op == "release":
            selector.release(*args)
        elif op == "drain":
            selector.drain(*args)
        elif op == "restore":
            selector.restore(*args)
        elif op == "set_weight":
            selector.set_weight(*args)
        elif op == "add":
            selector.add_node(*args)
        elif op == "remove":
            selector.remove_node(*args)
    return out


def build(cls, spec):
    sel = cls()
    for nid, w in spec:
        sel.add_node(nid, w)
    return sel


class SmoothWRRTest(unittest.TestCase):
    def test_exact_weight_proportions(self):
        sel = build(SmoothWeightedRoundRobin, [("a", 5), ("b", 3), ("c", 2)])
        for _ in range(10000):
            sel.select()
        stats = sel.stats()
        self.assertEqual(stats["a"]["count"], 5000)
        self.assertEqual(stats["b"]["count"], 3000)
        self.assertEqual(stats["c"]["count"], 2000)

    def test_extreme_weight_gap(self):
        sel = build(SmoothWeightedRoundRobin, [("big", 1000), ("small", 1)])
        for _ in range(1001 * 10):
            sel.select()
        stats = sel.stats()
        self.assertEqual(stats["big"]["count"], 10000)
        self.assertEqual(stats["small"]["count"], 10)

    def test_smoothness_no_long_burst(self):
        # smooth WRR must interleave: with weights 5,1,1 the gap between
        # two picks of "b" is bounded; classic naive WRR would burst 5x a.
        sel = build(SmoothWeightedRoundRobin, [("a", 5), ("b", 1), ("c", 1)])
        seq = [sel.select() for _ in range(7)]
        self.assertEqual(seq.count("a"), 5)
        self.assertEqual(seq[0], "a")
        self.assertIn("b", seq[:5])  # b appears within first 5 picks

    def test_determinism_replay(self):
        spec = [("a", 3), ("b", 1), ("c", 2)]
        script = (
            [("select",)] * 4
            + [("drain", "a")]
            + [("select",)] * 3
            + [("restore", "a"), ("set_weight", "b", 5)]
            + [("select",)] * 6
            + [("remove", "c")]
            + [("select",)] * 4
        )
        r1 = run_script(build(SmoothWeightedRoundRobin, spec), script)
        r2 = run_script(build(SmoothWeightedRoundRobin, spec), script)
        self.assertEqual(r1, r2)

    def test_tie_break_insertion_order(self):
        sel = build(SmoothWeightedRoundRobin, [("z", 1), ("y", 1), ("x", 1)])
        # equal weights -> round robin in insertion order
        self.assertEqual([sel.select() for _ in range(3)], ["z", "y", "x"])

    def test_drained_node_never_selected_and_recovers(self):
        sel = build(SmoothWeightedRoundRobin, [("a", 1), ("b", 1)])
        sel.drain("a")
        for _ in range(20):
            self.assertEqual(sel.select(), "b")
        sel.restore("a")
        # bound: ceil(T/w) = ceil(2/1) = 2 selections to reappear
        seq = [sel.select() for _ in range(2)]
        self.assertIn("a", seq)

    def test_restore_bound_heavy_others(self):
        # restored node weight 1, others total 10 -> bound = ceil(11/1) = 11
        sel = build(SmoothWeightedRoundRobin, [("a", 10), ("b", 1)])
        sel.drain("b")
        for _ in range(5):
            sel.select()
        sel.restore("b")
        seq = [sel.select() for _ in range(11)]
        self.assertIn("b", seq)

    def test_zero_weight_never_selected(self):
        sel = build(SmoothWeightedRoundRobin, [("a", 1), ("dead", 0)])
        for _ in range(50):
            self.assertEqual(sel.select(), "a")

    def test_single_node(self):
        sel = build(SmoothWeightedRoundRobin, [("only", 7)])
        for _ in range(10):
            self.assertEqual(sel.select(), "only")

    def test_all_drained_raises(self):
        sel = build(SmoothWeightedRoundRobin, [("a", 1), ("b", 1)])
        sel.drain("a")
        sel.drain("b")
        with self.assertRaises(NoBackendAvailableError):
            sel.select()

    def test_all_zero_weight_raises(self):
        sel = build(SmoothWeightedRoundRobin, [("a", 0), ("b", 0)])
        with self.assertRaises(NoBackendAvailableError):
            sel.select()

    def test_dynamic_membership(self):
        sel = build(SmoothWeightedRoundRobin, [("a", 1)])
        self.assertEqual(sel.select(), "a")
        sel.add_node("b", 3)
        sel.set_weight("a", 1)
        counts = {"a": 0, "b": 0}
        for _ in range(400):
            counts[sel.select()] += 1
        self.assertEqual(counts, {"a": 100, "b": 300})
        sel.remove_node("b")
        for _ in range(10):
            self.assertEqual(sel.select(), "a")


class LeastConnectionsTest(unittest.TestCase):
    def test_spreads_without_release(self):
        sel = build(LeastConnections, [("a", 1), ("b", 1), ("c", 1)])
        seq = [sel.select() for _ in range(6)]
        self.assertEqual(sorted(seq), ["a", "a", "b", "b", "c", "c"])

    def test_prefers_idle_node(self):
        sel = build(LeastConnections, [("a", 1), ("b", 1)])
        sel.select()  # a (tie -> insertion order)
        self.assertEqual(sel.select(), "b")
        self.assertEqual(sel.select(), "a")  # both have 1 -> tie -> a

    def test_reflects_inflight_changes(self):
        sel = build(LeastConnections, [("a", 1), ("b", 1)])
        first = sel.select()  # a, now a=1 b=0
        self.assertEqual(sel.select(), "b")
        sel.release(first)
        # a back to 0, b=1 -> a wins
        self.assertEqual(sel.select(), "a")

    def test_weight_normalizes_capacity(self):
        sel = build(LeastConnections, [("big", 4), ("small", 1)])
        # score = active/weight. big(4) absorbs load until its score
        # reaches small's; ties break to insertion order ("big" first).
        picks = [sel.select() for _ in range(6)]
        self.assertEqual(picks, ["big", "small", "big", "big", "big", "big"])
        # big: 5/4 = 1.25 > small: 1/1 = 1.0 -> small next
        self.assertEqual(sel.select(), "small")

    def test_determinism_replay(self):
        spec = [("a", 2), ("b", 1), ("c", 3)]
        script = []
        for i in range(30):
            script.append(("select",))
            if i % 3 == 0:
                script.append(("release", "a"))
        script += [("drain", "c")] + [("select",)] * 4 + [("restore", "c")]
        script += [("select",)] * 5
        r1 = run_script(build(LeastConnections, spec), script)
        r2 = run_script(build(LeastConnections, spec), script)
        self.assertEqual(r1, r2)

    def test_drained_excluded_restored_rejoins_immediately(self):
        sel = build(LeastConnections, [("a", 1), ("b", 1)])
        sel.select()  # a=1
        sel.select()  # b=1
        sel.drain("b")
        for _ in range(5):
            self.assertEqual(sel.select(), "a")
        sel.restore("b")  # active reset to 0 -> minimum score
        self.assertEqual(sel.select(), "b")  # rejoins within 1 selection

    def test_zero_weight_never_selected(self):
        sel = build(LeastConnections, [("a", 1), ("dead", 0)])
        for _ in range(20):
            self.assertEqual(sel.select(), "a")

    def test_single_node(self):
        sel = build(LeastConnections, [("only", 1)])
        for _ in range(5):
            self.assertEqual(sel.select(), "only")

    def test_all_drained_raises(self):
        sel = build(LeastConnections, [("a", 1)])
        sel.drain("a")
        with self.assertRaises(NoBackendAvailableError):
            sel.select()

    def test_dynamic_weight_change(self):
        sel = build(LeastConnections, [("a", 1), ("b", 1)])
        sel.set_weight("b", 3)
        # b now tolerates 3x load; a picked once then b until ratio ties
        picks = [sel.select() for _ in range(4)]
        self.assertEqual(picks, ["a", "b", "b", "b"])


class ConcurrencyTest(unittest.TestCase):
    def _hammer(self, selector, threads, picks_per_thread, release=True):
        def worker():
            for _ in range(picks_per_thread):
                nid = selector.select()
                if release:
                    selector.release(nid)

        ts = [threading.Thread(target=worker) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    def test_concurrent_wrr_count_integrity(self):
        sel = build(SmoothWeightedRoundRobin, [("a", 5), ("b", 3), ("c", 2)])
        self._hammer(sel, threads=8, picks_per_thread=1250)  # total 10000
        stats = sel.stats()
        total = sum(s["count"] for s in stats.values())
        self.assertEqual(total, 10000)
        # smooth WRR stays exactly proportional even under concurrency
        self.assertEqual(stats["a"]["count"], 5000)
        self.assertEqual(stats["b"]["count"], 3000)
        self.assertEqual(stats["c"]["count"], 2000)

    def test_concurrent_least_connections_inflight_consistency(self):
        sel = build(LeastConnections, [("a", 1), ("b", 1), ("c", 1)])
        self._hammer(sel, threads=8, picks_per_thread=500, release=True)
        stats = sel.stats()
        self.assertEqual(sum(s["count"] for s in stats.values()), 4000)
        # every select was paired with a release
        for s in stats.values():
            self.assertEqual(s["active_connections"], 0)

    def test_concurrent_select_and_membership_change(self):
        sel = build(SmoothWeightedRoundRobin, [("a", 1), ("b", 1)])
        errors = []

        def worker():
            try:
                for _ in range(500):
                    sel.select()
            except NoBackendAvailableError:
                pass  # tolerated only if everything was drained
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def churn():
            for _ in range(50):
                sel.drain("b")
                sel.restore("b")

        ts = [threading.Thread(target=worker) for _ in range(4)]
        ts.append(threading.Thread(target=churn))
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])


class StatsTest(unittest.TestCase):
    def test_stats_shape(self):
        sel = build(SmoothWeightedRoundRobin, [("a", 3), ("b", 1)])
        for _ in range(4):
            sel.select()
        stats = sel.stats()
        self.assertEqual(stats["a"]["count"], 3)
        self.assertAlmostEqual(stats["a"]["share"], 0.75)
        self.assertAlmostEqual(stats["b"]["share"], 0.25)
        self.assertEqual(sel.total_selected(), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
