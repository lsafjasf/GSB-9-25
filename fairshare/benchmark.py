"""规模基准：1 万个租户的分配耗时。运行: python3 benchmark.py"""

import random
import time

from fairshare import allocate


def make_case(n, seed):
    rng = random.Random(seed)
    weights = [rng.choice([0, 1, 3, 10, 1000]) for _ in range(n)]
    demands = [rng.randint(0, 10_000) for _ in range(n)]
    minimums = [0 if w == 0 else rng.randint(0, d // 4)
                for w, d in zip(weights, demands)]
    capacity = sum(minimums) + int(sum(demands) * 0.6)  # 超限场景，触发削减
    return capacity, weights, demands, minimums


def main():
    n = 10_000
    capacity, weights, demands, minimums = make_case(n, seed=42)

    # 预热 + 计时（取 5 次最优，排除抖动）
    best = float("inf")
    alloc = None
    for _ in range(5):
        start = time.perf_counter()
        alloc = allocate(capacity, weights, demands, minimums)
        best = min(best, time.perf_counter() - start)

    reachable = sum(d if w > 0 else m
                    for w, d, m in zip(weights, demands, minimums))
    assert sum(alloc) == min(capacity, reachable)
    assert all(a <= d for a, d in zip(alloc, demands))
    assert all(a >= m for a, m in zip(alloc, minimums))
    assert all(a == 0 for a, w in zip(alloc, weights) if w == 0)

    print(f"tenants          : {n}")
    print(f"capacity         : {capacity}")
    print(f"total demand     : {sum(demands)}")
    print(f"total minimum    : {sum(minimums)}")
    print(f"allocated        : {sum(alloc)}")
    print(f"best of 5 runs   : {best*1000:.2f} ms")


if __name__ == "__main__":
    main()
