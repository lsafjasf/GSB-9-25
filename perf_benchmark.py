"""百万份规模性能基准。运行：python3 perf_benchmark.py [份数，默认 1000000]"""

import random
import resource
import sys
import time

sys.path.insert(0, "src")
from allocation import allocate


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    rng = random.Random(20260925)
    weights = [rng.randint(0, 10_000) for _ in range(n)]
    total = 10**12  # 1 万亿元（分）

    t0 = time.perf_counter()
    shares = allocate(total, weights)
    elapsed = time.perf_counter() - t0

    assert sum(s.amount for s in shares) == total
    assert all(s.amount == 0 for s in shares if s.weight == 0)
    carriers = sum(1 for s in shares if s.carried_remainder)
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    print(f"份数 n        = {n:,}")
    print(f"总额 total    = {total:,} 分")
    print(f"耗时          = {elapsed:.3f} s")
    print(f"吞吐          = {n / elapsed:,.0f} 份/s")
    print(f"峰值内存      = {peak_kb / 1024:.1f} MiB")
    print(f"余数承担份数  = {carriers:,}")
    print("不变量校验    = 通过（总和严格相等、零权重分得零）")


if __name__ == "__main__":
    main()
