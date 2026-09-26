"""十万任务调度性能基准（时间由 FakeClock 注入）。

场景一：全部可完成——乱序提交 10 万任务，依次出队，验证 EDF 顺序并计时。
场景二：时钟一次跳跃使 10 万任务全部超期——计时 tick 的批量驱逐与告警。
"""

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduler import Scheduler, FakeClock

N = 100_000


def scenario_feasible():
    clock = FakeClock(0)
    sched = Scheduler(clock)
    rng = random.Random(42)
    deadlines = list(range(1, N + 1))
    rng.shuffle(deadlines)

    t0 = time.perf_counter()
    for i, dl in enumerate(deadlines):
        sched.submit("t%d" % i, duration=1, deadline=float(dl))
    submit_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    prev_deadline = 0
    count = 0
    while True:
        result = sched.dequeue()
        if result is None or result.task is None:
            break
        assert result.task.deadline >= prev_deadline, "出队顺序违反 EDF"
        prev_deadline = result.task.deadline
        count += 1
    dequeue_elapsed = time.perf_counter() - t0
    return count, submit_elapsed, dequeue_elapsed


def scenario_all_overdue():
    clock = FakeClock(0)
    sched = Scheduler(clock)
    for i in range(N):
        sched.submit("t%d" % i, duration=1, deadline=float(i + 1))

    clock.jump_to(float(N + 10))  # 一次跳跃跨越全部任务
    t0 = time.perf_counter()
    overdue = sched.tick()
    tick_elapsed = time.perf_counter() - t0
    return len(overdue), sched.alert_count(), tick_elapsed


def main():
    print("== 十万任务 (N=%s) 性能基准 ==" % format(N, ","))

    count, submit_ms, dequeue_ms = scenario_feasible()
    total = submit_ms + dequeue_ms
    sub_us = submit_ms / N * 1e6
    deq_us = dequeue_ms / N * 1e6
    throughput = N / total
    print("[场景一] 全部可完成，乱序提交后按 EDF 出队")
    print("  出队任务数 : %s" % format(count, ","))
    print("  提交耗时   : %10.2f ms  (%7.2f us/任务)" % (submit_ms * 1000, sub_us))
    print("  出队耗时   : %10.2f ms  (%7.2f us/任务)" % (dequeue_ms * 1000, deq_us))
    print("  合计耗时   : %10.2f ms  吞吐 %s 任务/秒"
          % (total * 1000, format(round(throughput), ",")))
    assert count == N

    evicted, alerts, tick_ms = scenario_all_overdue()
    tick_throughput = N / tick_ms
    print("[场景二] 时钟一次跳跃，全部任务超期驱逐")
    print("  超期任务数 : %s" % format(evicted, ","))
    print("  告警总数   : %s（每任务恰好一次）" % format(alerts, ","))
    print("  tick 耗时  : %10.2f ms  吞吐 %s 任务/秒"
          % (tick_ms * 1000, format(round(tick_throughput), ",")))
    assert evicted == N and alerts == N


if __name__ == "__main__":
    main()
