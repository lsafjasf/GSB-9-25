"""百万份规模性能测试。运行：python3 perf.py"""
import random
import time

from allocate import allocate

for n in (10_000, 100_000, 1_000_000):
    rng = random.Random(12345)
    weights = [rng.randint(0, 10**6) for _ in range(n)]
    weights[0] = max(weights[0], 1)  # 保证权重和非零
    total = 10**12 - 7

    t0 = time.perf_counter()
    shares = allocate(total, weights)
    elapsed = time.perf_counter() - t0

    assert sum(s.amount for s in shares) == total
    residual = sum(1 for s in shares if s.takes_residual)
    print(f"n={n:>9,}  耗时 {elapsed*1000:8.1f} ms  "
          f"余数承担份数={residual}  总额校验=OK")
