"""Benchmark: 100k tasks through the EDF scheduler. Run: python3 bench.py"""

import random
import time

from edf_scheduler import Scheduler, Task

N = 100_000


def bench(seed=42):
    rng = random.Random(seed)
    now = [0.0]
    sched = Scheduler(clock=lambda: now[0])

    t0 = time.perf_counter()
    for i in range(N):
        # 90% with deadline, 10% without
        if rng.random() < 0.9:
            sched.submit(Task(f"t{i}", estimated_duration=1,
                              deadline=rng.uniform(0, N)))
        else:
            sched.submit(Task(f"t{i}", estimated_duration=1))
    t1 = time.perf_counter()

    dequeued = 0
    while sched.dequeue() is not None:
        dequeued += 1
    t2 = time.perf_counter()

    print(f"tasks            : {N:,}")
    print(f"submit phase     : {t1 - t0:.3f}s ({N / (t1 - t0):,.0f} ops/s)")
    print(f"dequeue phase    : {t2 - t1:.3f}s ({N / (t2 - t1):,.0f} ops/s)")
    print(f"total            : {t2 - t0:.3f}s")
    print(f"dequeued         : {dequeued:,}  alerts: {sched.alert_count:,}")


if __name__ == "__main__":
    bench()
