"""统计输出样例：python3 demo.py"""

import threading
import time

from rate_limiter import SlidingWindowLimiter, TokenBucketLimiter


def hammer(limiter, threads=8, attempts=50):
    barrier = threading.Barrier(threads)

    def worker():
        barrier.wait()
        for _ in range(attempts):
            limiter.allow()

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def show(name, limiter):
    s = limiter.stats()
    print(f"[{name}]")
    print(f"  放行数 allowed      = {s.allowed}")
    print(f"  拒绝数 rejected     = {s.rejected}")
    print(f"  当前水位 level      = {s.level:.2f}")
    print(f"  平均等待 avg_wait   = {s.average_wait*1000:.2f} ms")


bucket = TokenBucketLimiter(capacity=20, refill_rate=50)  # 突发 20，长期 50/s
window = SlidingWindowLimiter(limit=20, window=1.0)       # 任意 1s 内至多 20 次

hammer(bucket)
time.sleep(0.05)  # 让桶补充一点，水位更直观
hammer(window)

show("TokenBucket capacity=20 rate=50/s", bucket)
show("SlidingWindow limit=20 window=1s", window)
