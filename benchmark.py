"""Scale benchmark: allocation time for up to 10,000+ tenants."""

import random
import time
from fractions import Fraction

from quota import Tenant, allocate


def make_tenants(n, seed=7):
    rng = random.Random(seed)
    tenants = []
    for i in range(n):
        demand = rng.randint(100, 10_000)
        guarantee = rng.randint(0, demand // 4)
        weight = rng.randint(1, 1000)
        tenants.append(Tenant(weight, demand, guarantee, f"tenant-{i}"))
    return tenants


def check_invariants(capacity, tenants, result):
    total_demand = sum(t.demand for t in tenants)
    assert sum(a.share for a in result) == min(capacity, total_demand)
    for t, a in zip(tenants, result):
        assert a.guarantee <= a.share <= t.demand
    print("  invariants: OK (conservation, demand cap, guarantee floor)")


def main():
    for n in (1_000, 10_000, 20_000):
        tenants = make_tenants(n)
        total_demand = sum(t.demand for t in tenants)
        capacity = int(total_demand * 0.6)  # oversubscribed: forces cutbacks

        # Warm-up + best of 3 for a stable figure.
        times = []
        for _ in range(3):
            start = time.perf_counter()
            result = allocate(capacity, tenants)
            times.append(time.perf_counter() - start)
        best = min(times)
        print(
            f"n={n:>6} tenants: {best*1000:8.2f} ms  "
            f"(capacity={capacity}, total_demand={total_demand})"
        )
        if n == 10_000:
            check_invariants(capacity, tenants, result)
            sample = result[0]
            print(
                f"  sample share: {sample.name} -> {float(sample.share):.2f} "
                f"(exact: {sample.share})"
            )


if __name__ == "__main__":
    main()
