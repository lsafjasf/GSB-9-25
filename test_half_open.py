"""Tests for the half-open recovery state machine and recovery bounds.

Stdlib unittest only. A scripted clock makes every scenario reproducible.
"""

import itertools
import unittest

from backend_selector import (
    HALF_OPEN,
    HEALTHY,
    OPEN,
    LeastConnections,
    NoBackendAvailableError,
    SmoothWeightedRoundRobin,
)


class FakeClock:
    """Deterministic, externally advanced clock."""

    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def replay(cls, script, clock, *, cooldown=10.0, required_successes=2):
    """Replay a deterministic op script against a scripted clock."""
    sel = cls(cooldown=cooldown, required_successes=required_successes,
              clock=clock)
    out = []
    for op, *args in script:
        if op == "add":
            sel.add_node(*args)
        elif op == "select":
            out.append(sel.select())
        elif op == "select_probe":
            out.append(("probe", sel.select_probe()))
        elif op == "report":
            out.append(("state", sel.report_probe(*args)))
        elif op == "tick":
            clock.advance(args[0] if args else 0)
            sel.health_tick()
        elif op == "advance":
            clock.advance(args[0])
        elif op == "drain":
            sel.drain(*args)
        elif op == "restore":
            sel.restore(*args)
        elif op == "set_weight":
            sel.set_weight(*args)
    return sel, out


class HalfOpenLifecycleTest(unittest.TestCase):
    def _new(self, cls, cooldown=10.0, required=2):
        clock = FakeClock()
        sel = cls(cooldown=cooldown, required_successes=required, clock=clock)
        sel.add_node("a", 1)
        sel.add_node("b", 1)
        return sel, clock

    def test_open_node_never_selected_even_if_only_candidate_weight(self):
        for cls in (SmoothWeightedRoundRobin, LeastConnections):
            sel, clock = self._new(cls)
            sel.drain("a")
            sel.drain("b")
            for _ in range(30):
                with self.assertRaises(NoBackendAvailableError):
                    sel.select()
            self.assertIsNone(sel.select_probe())  # cooldown not elapsed

    def test_cooldown_then_single_probe_half_open_excluded(self):
        for cls in (SmoothWeightedRoundRobin, LeastConnections):
            sel, clock = self._new(cls)
            sel.drain("b")
            # while OPEN, only a gets normal traffic
            for _ in range(20):
                self.assertEqual(sel.select(), "a")
            self.assertEqual(sel.state_of("b"), OPEN)
            self.assertIsNone(sel.select_probe())
            clock.advance(9)
            self.assertIsNone(sel.select_probe())       # still cooling
            clock.advance(1)
            # exactly one probe is handed out
            self.assertEqual(sel.select_probe(), "b")
            self.assertIsNone(sel.select_probe())       # gate: 1 at a time
            # half-open node never serves normal traffic
            for _ in range(20):
                self.assertEqual(sel.select(), "a")
            self.assertEqual(sel.state_of("b"), HALF_OPEN)

    def test_failed_probe_reopens_cooldown_restarts(self):
        for cls in (SmoothWeightedRoundRobin, LeastConnections):
            sel, clock = self._new(cls)
            sel.drain("b")
            clock.advance(10)
            self.assertEqual(sel.select_probe(), "b")
            self.assertEqual(sel.report_probe("b", False), OPEN)
            self.assertIsNone(sel.select_probe())
            clock.advance(9)
            self.assertIsNone(sel.select_probe())
            clock.advance(1)
            self.assertEqual(sel.select_probe(), "b")

    def test_consecutive_successes_required_not_cumulative(self):
        for cls in (SmoothWeightedRoundRobin, LeastConnections):
            sel, clock = self._new(cls, required=3)
            sel.drain("b")
            clock.advance(10)
            self.assertEqual(sel.select_probe(), "b")
            self.assertEqual(sel.report_probe("b", True), HALF_OPEN)
            clock.advance(0.001)
            self.assertEqual(sel.select_probe(), "b")
            self.assertEqual(sel.report_probe("b", False), OPEN)  # resets
            clock.advance(10)
            # must earn 3 fresh consecutive successes
            results = []
            for _ in range(3):
                self.assertEqual(sel.select_probe(), "b")
                results.append(sel.report_probe("b", True))
            self.assertEqual(results, [HALF_OPEN, HALF_OPEN, HEALTHY])

    def test_half_open_at_boundary_second_node_waits_its_turn(self):
        sel, clock = self._new(SmoothWeightedRoundRobin)
        sel.drain("a")
        sel.drain("b")
        clock.advance(10)
        self.assertEqual(sel.select_probe(), "a")  # insertion order
        # gate is per-node: b may hold its own probe in parallel
        self.assertEqual(sel.select_probe(), "b")
        self.assertIsNone(sel.select_probe())      # but no second one each
        self.assertEqual(sel.report_probe("a", False), OPEN)
        self.assertEqual(sel.report_probe("b", False), OPEN)
        # a was drained first (cooldown ends earlier): it gets the probe
        clock.advance(10)
        self.assertEqual(sel.select_probe(), "a")

    def test_zero_weight_never_probed_or_selected(self):
        for cls in (SmoothWeightedRoundRobin, LeastConnections):
            sel, clock = self._new(cls)
            sel.set_weight("b", 0)
            sel.drain("b")
            clock.advance(100)
            self.assertIsNone(sel.select_probe())   # weight 0: no probe
            for _ in range(30):
                self.assertEqual(sel.select(), "a")
            # raising weight after cooldown still requires a probe first
            sel.set_weight("b", 1)
            self.assertEqual(sel.select_probe(), "b")

    def test_zero_weight_node_excluded_after_full_recovery(self):
        for cls in (SmoothWeightedRoundRobin, LeastConnections):
            sel, clock = self._new(cls, required=1)
            sel.drain("b")
            clock.advance(10)
            sel.select_probe()
            sel.report_probe("b", True)
            self.assertEqual(sel.state_of("b"), HEALTHY)
            sel.set_weight("b", 0)
            for _ in range(20):
                self.assertEqual(sel.select(), "a")
            self.assertIsNone(sel.recovery_bound("b"))
            sel.set_weight("b", 1)
            # participates again right away (never left HEALTHY state)
            seq = [sel.select() for _ in range(2)]
            self.assertIn("b", seq)

    def test_run_health_checks_full_pass(self):
        for cls in (SmoothWeightedRoundRobin, LeastConnections):
            sel, clock = self._new(cls, required=2)
            sel.drain("b")
            clock.advance(10)
            outcomes = iter([False, True, True])
            # pass 1: probe fails -> re-open
            r1 = sel.run_health_checks(lambda nid: next(outcomes))
            self.assertEqual(r1, {"b": OPEN})
            clock.advance(10)
            # pass 2: one success only -> stays half open (1 probe/pass)
            r2 = sel.run_health_checks(lambda nid: next(outcomes))
            self.assertEqual(r2, {"b": HALF_OPEN})
            # pass 3: second consecutive success -> healthy
            r3 = sel.run_health_checks(lambda nid: next(outcomes))
            self.assertEqual(r3, {"b": HEALTHY})

    def test_stale_report_ignored(self):
        sel, clock = self._new(SmoothWeightedRoundRobin, required=1)
        sel.drain("b")
        clock.advance(10)
        sel.select_probe()
        sel.drain("b")  # node re-drained while probe was in flight
        self.assertEqual(sel.report_probe("b", True), OPEN)
        self.assertEqual(sel.state_of("b"), OPEN)


class RecoveryBoundTest(unittest.TestCase):
    """Assert the advertised re-participation upper bounds hold."""

    def test_swrr_bound_ceil_T_over_w_exhaustive(self):
        # Exhaust over small weight profiles: drain each node in turn for
        # varying numbers of selections, recover, and check it is picked
        # within ceil(T / w) selections -- tight against the bound.
        weight_sets = [(1, 1), (2, 3), (5, 3, 2), (10, 1),
                       (4, 4, 1), (1, 1, 1, 1), (7, 2)]
        for weights in weight_sets:
            total = sum(weights)
            for victim in range(len(weights)):
                w = weights[victim]
                bound = -(-total // w)
                for gap in range(0, 3 * total + 1):
                    sel = SmoothWeightedRoundRobin()
                    ids = [f"n{i}" for i in range(len(weights))]
                    for nid, ww in zip(ids, weights):
                        sel.add_node(nid, ww)
                    vid = ids[victim]
                    sel.drain(vid)
                    for _ in range(gap):
                        sel.select()
                    sel.restore(vid)
                    seq = [sel.select() for _ in range(bound)]
                    self.assertIn(
                        vid, seq,
                        f"weights={weights} victim={vid} gap={gap} "
                        f"bound={bound}",
                    )

    def test_swrr_recovery_bound_method(self):
        sel = SmoothWeightedRoundRobin()
        sel.add_node("a", 10)
        sel.add_node("b", 1)
        self.assertEqual(sel.recovery_bound("b"), 11)  # ceil(11/1)
        self.assertEqual(sel.recovery_bound("a"), 2)   # ceil(11/10)
        sel.set_weight("b", 5)
        self.assertEqual(sel.recovery_bound("b"), 3)   # ceil(15/5)

    def test_lc_recovered_node_picked_on_next_select(self):
        # Even under adversarial releases, recovered node wins immediately.
        sel = LeastConnections()
        sel.add_node("a", 1)
        sel.add_node("b", 1)
        sel.add_node("c", 1)
        sel.drain("c")
        for _ in range(50):
            nid = sel.select()
            sel.release(nid)  # everyone idle -> all scores 0
        sel.restore("c")
        self.assertEqual(sel.recovery_bound("c"), 1)
        self.assertEqual(sel.select(), "c")
        # priority was single-shot; afterwards insertion-order ties resume
        sel.release("c")
        self.assertEqual(sel.select(), "a")

    def test_lc_half_open_recovery_via_probes_then_immediate(self):
        clock = FakeClock()
        sel = LeastConnections(cooldown=5.0, required_successes=2,
                               clock=clock)
        sel.add_node("a", 1)
        sel.add_node("b", 1)
        sel.drain("b")
        for _ in range(10):
            sel.select()
        clock.advance(5)
        sel.select_probe()
        sel.report_probe("b", True)
        clock.advance(0.1)
        sel.select_probe()
        sel.report_probe("b", True)
        self.assertEqual(sel.state_of("b"), HEALTHY)
        self.assertEqual(sel.select(), "b")  # within bound = 1


class DeterminismWithClockTest(unittest.TestCase):
    SCRIPT = [
        ("add", "a", 5), ("add", "b", 3), ("add", "c", 2),
        ("select",), ("select",), ("select",),
        ("drain", "b"),
        ("select",), ("select",),
        ("advance", 10),
        ("select_probe",),
        ("report", "b", True),
        ("advance", 0.5),
        ("select_probe",),
        ("report", "b", False),
        ("advance", 10),
        ("select_probe",),
        ("report", "b", True),
        ("set_weight", "c", 7),
        ("advance", 0.5),
        ("select_probe",),
        ("report", "b", True),
        ("select",), ("select",), ("select",), ("select",),
    ]

    def test_same_script_same_result_both_strategies(self):
        for cls in (SmoothWeightedRoundRobin, LeastConnections):
            _, r1 = replay(cls, self.SCRIPT, FakeClock())
            _, r2 = replay(cls, self.SCRIPT, FakeClock())
            self.assertEqual(r1, r2)

    def test_runtime_weight_change_reproducible_distribution(self):
        def run():
            sel = SmoothWeightedRoundRobin()
            sel.add_node("a", 1)
            sel.add_node("b", 1)
            seq = [sel.select() for _ in range(4)]
            sel.set_weight("b", 3)
            seq += [sel.select() for _ in range(400)]
            return seq, sel.stats()

        seq1, stats1 = run()
        seq2, stats2 = run()
        self.assertEqual(seq1, seq2)
        # first 4 picks happened at 1:1; afterwards weights were 1:3
        self.assertEqual(stats1, stats2)
        after = {"a": 0, "b": 0}
        for nid in seq1[4:]:
            after[nid] += 1
        self.assertEqual(after, {"a": 100, "b": 300})

    def test_bound_is_respected_after_probe_driven_recovery(self):
        # End-to-end: half-open recovery + advertised SWRR bound.
        for required in (1, 2, 3):
            clock = FakeClock()
            sel = SmoothWeightedRoundRobin(
                cooldown=5.0, required_successes=required, clock=clock)
            sel.add_node("a", 10)
            sel.add_node("b", 1)
            sel.drain("b")
            for _ in range(7):
                sel.select()
            for _ in range(required):
                clock.advance(5)
                self.assertEqual(sel.select_probe(), "b")
                self.assertNotIn("b", [sel.select() for _ in range(3)])
                self.assertEqual(sel.report_probe("b", True),
                                 HEALTHY if _ + 1 == required else HALF_OPEN)
            bound = sel.recovery_bound("b")
            self.assertEqual(bound, 11)
            seq = [sel.select() for _ in range(bound)]
            self.assertIn("b", seq)


if __name__ == "__main__":
    unittest.main(verbosity=2)
