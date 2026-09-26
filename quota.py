"""Weighted fair allocation of a shared resource with per-tenant guarantees.

Algorithm: weighted water-filling over residual demands.

  1. Reserve each tenant's minimum guarantee (validated to be feasible).
  2. Distribute the remaining capacity proportionally to weights, capped by
     each tenant's residual demand (demand - guarantee). Tenants saturate in
     increasing order of residual/weight; a single sorted sweep finds the
     water level, giving O(n log n) time and O(n) extra space.
  3. Zero-weight tenants receive exactly zero residual share.

All arithmetic uses fractions.Fraction, so results are exact and fully
deterministic: identical inputs always produce identical outputs.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import List, Sequence, Union

Number = Union[int, Fraction]

__all__ = ["Tenant", "Allocation", "InfeasibleError", "allocate"]


class InfeasibleError(Exception):
    """Raised when the sum of minimum guarantees exceeds capacity."""

    def __init__(self, capacity: Number, total_guarantee: Fraction, parties: Sequence[str]):
        self.capacity = Fraction(capacity)
        self.total_guarantee = total_guarantee
        self.deficit = total_guarantee - self.capacity
        self.parties = list(parties)
        super().__init__(
            "infeasible guarantees: sum of minimum guarantees "
            f"{total_guarantee} exceeds capacity {self.capacity} "
            f"(deficit {self.deficit}); conflicting parties: "
            + ", ".join(self.parties)
        )


@dataclass(frozen=True)
class Tenant:
    """A tenant requesting a share of the resource.

    weight:    relative fair-share weight (0 means "gets nothing beyond
               guarantee", and a zero-weight tenant must have guarantee 0).
    demand:    maximum amount the tenant can use (hard cap).
    guarantee: minimum amount the tenant must receive.
    name:      identifier used in error messages.
    """

    weight: Number
    demand: Number
    guarantee: Number = 0
    name: str = ""


@dataclass(frozen=True)
class Allocation:
    """Result for one tenant: final share and its guarantee/residual split."""

    name: str
    share: Fraction
    guarantee: Fraction
    residual: Fraction


def _validate(capacity: Number, tenants: Sequence[Tenant]) -> None:
    if Fraction(capacity) < 0:
        raise ValueError(f"capacity must be >= 0, got {capacity}")
    for i, t in enumerate(tenants):
        label = t.name or f"#{i}"
        if Fraction(t.weight) < 0:
            raise ValueError(f"tenant {label}: negative weight {t.weight}")
        if Fraction(t.demand) < 0:
            raise ValueError(f"tenant {label}: negative demand {t.demand}")
        if Fraction(t.guarantee) < 0:
            raise ValueError(f"tenant {label}: negative guarantee {t.guarantee}")
        if Fraction(t.guarantee) > Fraction(t.demand):
            raise ValueError(
                f"tenant {label}: guarantee {t.guarantee} exceeds demand {t.demand}"
            )
        if Fraction(t.weight) == 0 and Fraction(t.guarantee) > 0:
            raise ValueError(
                f"tenant {label}: zero weight cannot carry a positive guarantee "
                f"({t.guarantee}); the invariant 'zero weight -> zero share' "
                "would be violated"
            )


def allocate(capacity: Number, tenants: Sequence[Tenant]) -> List[Allocation]:
    """Distribute ``capacity`` among ``tenants`` by weighted fair share.

    Returns one Allocation per tenant, in input order. Guarantees:
      * sum of shares == min(capacity, sum of demands)   (conservation)
      * guarantee <= share <= demand for every tenant
      * weight == 0  =>  share == 0
    Raises InfeasibleError if the guarantees alone exceed capacity.
    """
    _validate(capacity, tenants)
    cap = Fraction(capacity)
    n = len(tenants)

    guarantees = [Fraction(t.guarantee) for t in tenants]
    total_guarantee = sum(guarantees, Fraction(0))
    if total_guarantee > cap:
        parties = [
            t.name or f"#{i}" for i, t in enumerate(tenants) if Fraction(t.guarantee) > 0
        ]
        raise InfeasibleError(cap, total_guarantee, parties)

    remaining = cap - total_guarantee
    residuals = [Fraction(0)] * n

    # Active tenants: positive weight and unsatisfied residual demand.
    active = [
        (Fraction(t.weight), Fraction(t.demand) - guarantees[i], i)
        for i, t in enumerate(tenants)
        if Fraction(t.weight) > 0 and Fraction(t.demand) > guarantees[i]
    ]

    if active and remaining > 0:
        # Saturation level of tenant i is residual_i / weight_i: the water
        # level at which its residual demand is exactly met. Process tenants
        # in increasing saturation level; the index tiebreak keeps the order
        # total and therefore deterministic.
        keyed = sorted(
            ((res / w, w, res, i) for w, res, i in active),
            key=lambda item: (item[0], item[3]),
        )
        level = Fraction(0)          # current water level (share per unit weight)
        active_weight = sum((w for _, w, _, _ in keyed), Fraction(0))
        pos = 0
        while pos < len(keyed) and remaining > 0:
            next_level = keyed[pos][0]
            # Raising all active tenants from `level` to `next_level` costs:
            cost = (next_level - level) * active_weight
            if cost >= remaining:
                # Capacity runs out inside this band: stop at a partial level.
                level += remaining / active_weight
                remaining = Fraction(0)
                break
            # Saturate every tenant whose level is exactly `next_level`.
            remaining -= cost
            level = next_level
            while pos < len(keyed) and keyed[pos][0] == next_level:
                _, w, res, i = keyed[pos]
                residuals[i] = res
                active_weight -= w
                pos += 1
        if remaining == Fraction(0):
            # Everyone not yet saturated gets weight * final level.
            for _, w, _, i in keyed[pos:]:
                residuals[i] = w * level
        # else: all demands saturated; leftover capacity is simply not usable
        # (total demand < capacity), conservation holds against sum(demands).

    return [
        Allocation(
            name=t.name or f"#{i}",
            share=guarantees[i] + residuals[i],
            guarantee=guarantees[i],
            residual=residuals[i],
        )
        for i, t in enumerate(tenants)
    ]
