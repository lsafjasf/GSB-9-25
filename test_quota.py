"""Invariant and scenario tests for quota.allocate (stdlib unittest)."""

import random
import unittest
from fractions import Fraction

from quota import Allocation, InfeasibleError, Tenant, allocate


def assert_invariants(test_case, capacity, tenants, result):
    """The four allocation invariants, checked exactly (Fraction arithmetic)."""
    cap = Fraction(capacity)
    # Zero-weight tenants can never receive a share, so only the demand of
    # positive-weight tenants (plus zero-weight guarantees, always 0) counts
    # toward the allocatable total.
    total_demand = sum(
        (Fraction(t.demand) for t in tenants if Fraction(t.weight) > 0), Fraction(0)
    ) + sum(
        (Fraction(t.guarantee) for t in tenants if Fraction(t.weight) == 0), Fraction(0)
    )

    # 1. Conservation: shares sum to the allocatable total.
    allocatable = min(cap, total_demand)
    total_share = sum((a.share for a in result), Fraction(0))
    test_case.assertEqual(
        total_share,
        allocatable,
        f"conservation violated: sum(shares)={total_share} != {allocatable}",
    )

    for t, a in zip(tenants, result):
        # 2. No tenant exceeds its own demand.
        test_case.assertLessEqual(a.share, Fraction(t.demand), f"{a.name} over demand")
        # 3. No tenant falls below its minimum guarantee.
        test_case.assertGreaterEqual(
            a.share, Fraction(t.guarantee), f"{a.name} under guarantee"
        )
        # 4. Zero weight means zero share.
        if Fraction(t.weight) == 0:
            test_case.assertEqual(a.share, Fraction(0), f"{a.name} has weight 0")
    # Result lines up with input order and decomposes correctly.
    for a in result:
        test_case.assertEqual(a.share, a.guarantee + a.residual)


class ScenarioTests(unittest.TestCase):
    def test_single_tenant_capped_by_demand(self):
        tenants = [Tenant(weight=5, demand=30, guarantee=10, name="solo")]
        result = allocate(100, tenants)
        self.assertEqual(result[0].share, 30)  # demand cap, not capacity
        assert_invariants(self, 100, tenants, result)

    def test_single_tenant_capacity_binding(self):
        tenants = [Tenant(weight=1, demand=500, guarantee=100, name="solo")]
        result = allocate(200, tenants)
        self.assertEqual(result[0].share, 200)
        assert_invariants(self, 200, tenants, result)

    def test_total_demand_below_capacity(self):
        tenants = [
            Tenant(weight=1, demand=10, guarantee=2, name="a"),
            Tenant(weight=3, demand=20, guarantee=5, name="b"),
            Tenant(weight=0, demand=7, guarantee=0, name="c"),
        ]
        result = allocate(1000, tenants)
        # c has weight 0 and can never receive anything, so its demand is
        # not allocatable; a and b are fully satisfied.
        self.assertEqual([a.share for a in result], [10, 20, 0])
        assert_invariants(self, 1000, tenants, result)

    def test_all_demands_zero(self):
        tenants = [
            Tenant(weight=1, demand=0, name="a"),
            Tenant(weight=9, demand=0, name="b"),
        ]
        result = allocate(100, tenants)
        self.assertEqual([a.share for a in result], [0, 0])
        assert_invariants(self, 100, tenants, result)

    def test_zero_capacity(self):
        tenants = [Tenant(weight=1, demand=10, name="a")]
        result = allocate(0, tenants)
        self.assertEqual(result[0].share, 0)
        assert_invariants(self, 0, tenants, result)

    def test_equal_weights_split_evenly(self):
        tenants = [Tenant(weight=1, demand=100, name=n) for n in "abcd"]
        result = allocate(100, tenants)
        self.assertEqual([a.share for a in result], [25, 25, 25, 25])
        assert_invariants(self, 100, tenants, result)

    def test_equal_weights_with_uneven_split_is_exact(self):
        tenants = [Tenant(weight=1, demand=100, name=n) for n in "abc"]
        result = allocate(100, tenants)
        self.assertEqual(
            [a.share for a in result],
            [Fraction(100, 3)] * 3,
        )
        assert_invariants(self, 100, tenants, result)

    def test_extreme_weight_disparity(self):
        tenants = [
            Tenant(weight=10**9, demand=10**9, name="whale"),
            Tenant(weight=1, demand=10**9, name="minnow"),
        ]
        result = allocate(10**9 + 1, tenants)
        whale, minnow = (a.share for a in result)
        # Water level is exactly 1: whale saturates at its demand, minnow gets 1.
        self.assertEqual(whale, 10**9)
        self.assertEqual(minnow, 1)
        self.assertGreater(whale, minnow)
        assert_invariants(self, 10**9 + 1, tenants, result)

    def test_weight_priority_but_demand_cap_respected(self):
        # Heavy tenant saturates at its small demand; the rest flows to others.
        tenants = [
            Tenant(weight=100, demand=5, name="heavy-small"),
            Tenant(weight=1, demand=1000, name="light-big"),
        ]
        result = allocate(100, tenants)
        self.assertEqual(result[0].share, 5)
        self.assertEqual(result[1].share, 95)
        assert_invariants(self, 100, tenants, result)

    def test_guarantees_survive_scarcity(self):
        tenants = [
            Tenant(weight=100, demand=1000, guarantee=0, name="greedy"),
            Tenant(weight=1, demand=100, guarantee=40, name="protected"),
        ]
        result = allocate(50, tenants)
        # Guarantee intact; the 10 units of leftover split 100:1 by weight.
        self.assertEqual(result[1].share, 40 + Fraction(10, 101))
        self.assertEqual(result[0].share, Fraction(1000, 101))
        self.assertGreaterEqual(result[1].share, 40)
        assert_invariants(self, 50, tenants, result)

    def test_zero_weight_gets_zero(self):
        tenants = [
            Tenant(weight=0, demand=1000, name="weightless"),
            Tenant(weight=1, demand=1000, name="normal"),
        ]
        result = allocate(10, tenants)
        self.assertEqual(result[0].share, 0)
        self.assertEqual(result[1].share, 10)
        assert_invariants(self, 10, tenants, result)

    def test_determinism_repeated_runs(self):
        rng = random.Random(42)
        tenants = [
            Tenant(
                weight=rng.randint(0, 50),
                demand=rng.randint(0, 500),
                guarantee=0,
                name=f"t{i}",
            )
            for i in range(200)
        ]
        tenants = [
            Tenant(
                t.weight,
                t.demand,
                0 if t.weight == 0 else rng.randint(0, t.demand // 2),
                t.name,
            )
            for t in tenants
        ]
        first = allocate(30_000, tenants)
        for _ in range(5):
            again = allocate(30_000, tenants)
            self.assertEqual(first, again)
        assert_invariants(self, 30_000, tenants, first)

    def test_fractional_inputs(self):
        tenants = [
            Tenant(weight=Fraction(1, 2), demand=Fraction(7, 3), guarantee=Fraction(1, 3), name="x"),
            Tenant(weight=Fraction(3, 2), demand=10, name="y"),
        ]
        result = allocate(Fraction(5, 2), tenants)
        assert_invariants(self, Fraction(5, 2), tenants, result)


class InfeasibleTests(unittest.TestCase):
    def test_guarantees_exceeding_capacity_fail_loudly(self):
        tenants = [
            Tenant(weight=1, demand=100, guarantee=60, name="alpha"),
            Tenant(weight=1, demand=100, guarantee=50, name="beta"),
            Tenant(weight=1, demand=100, guarantee=0, name="gamma"),
        ]
        with self.assertRaises(InfeasibleError) as ctx:
            allocate(100, tenants)
        err = ctx.exception
        self.assertEqual(err.deficit, 10)                    # exact shortfall
        self.assertEqual(err.parties, ["alpha", "beta"])     # conflicting parties
        self.assertEqual(err.total_guarantee, 110)
        self.assertIn("deficit 10", str(err))

    def test_guarantee_above_demand_rejected(self):
        with self.assertRaises(ValueError):
            allocate(10, [Tenant(weight=1, demand=5, guarantee=6, name="bad")])

    def test_zero_weight_with_positive_guarantee_rejected(self):
        with self.assertRaises(ValueError):
            allocate(10, [Tenant(weight=0, demand=5, guarantee=1, name="bad")])

    def test_negative_fields_rejected(self):
        with self.assertRaises(ValueError):
            allocate(10, [Tenant(weight=-1, demand=5)])
        with self.assertRaises(ValueError):
            allocate(10, [Tenant(weight=1, demand=-5)])
        with self.assertRaises(ValueError):
            allocate(-1, [Tenant(weight=1, demand=5)])


class FuzzInvariantTests(unittest.TestCase):
    def test_randomized_invariants(self):
        rng = random.Random(20260926)
        for trial in range(300):
            n = rng.randint(1, 60)
            tenants = []
            for i in range(n):
                demand = rng.randint(0, 300)
                guarantee = rng.randint(0, demand)
                weight = rng.choice([0, 1, 2, 5, 17, 1000])
                if weight == 0:
                    guarantee = 0
                tenants.append(Tenant(weight, demand, guarantee, f"t{i}"))
            total_guarantee = sum(t.guarantee for t in tenants)
            # Mix feasible tight capacities with generous ones.
            capacity = rng.choice(
                [
                    total_guarantee,
                    total_guarantee + rng.randint(0, 5000),
                    rng.randint(0, 9000),
                ]
            )
            if capacity < total_guarantee:
                with self.assertRaises(InfeasibleError):
                    allocate(capacity, tenants)
            else:
                result = allocate(capacity, tenants)
                assert_invariants(self, capacity, tenants, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
