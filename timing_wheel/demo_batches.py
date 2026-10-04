"""演示并打印「休眠后的触发分批数据」。

运行：python3 -m timing_wheel.demo_batches
"""

from timing_wheel.clock import ManualClock
from timing_wheel.wheel import DROP, TimingWheel

TICK_MS = 100


def run_merge_demo():
    print("=" * 68)
    print("场景：5000 个任务在 1 小时休眠期间到期，单次触发上限 = 200")
    print("过期策略：merge（合并过期任务，逐批限速消化）")
    print("=" * 68)
    clock = ManualClock(start=1_700_000_000.0)
    wheel = TimingWheel(tick_ms=TICK_MS, now=clock, max_fires_per_advance=200)
    n = 5000
    for i in range(n):
        wheel.add(delay_ms=1000 + (i % 3_599_000), payload=f"task-{i}")

    clock.sleep(3600)  # 机器休眠
    batches = [wheel.advance()]
    while wheel.backlog_size:
        clock.tick(TICK_MS / 1000)
        batches.append(wheel.advance())

    print(f"{'批次':>4} {'时刻(s)':>10} {'本批触发':>8} {'剩余积压':>8} {'合并跳tick':>10}")
    wake_t = clock()
    for b in batches:
        t = wake_t + (b.index - 1) * TICK_MS / 1000
        if b.index <= 3 or b.index >= len(batches) - 1 or b.index == 4:
            print(f"{b.index:>4} {t - wake_t:>10.1f} {b.fired_count:>8} "
                  f"{b.remaining_backlog:>8} {b.skipped_ticks:>10}")
        elif b.index == 5:
            print("   ...")
    total = sum(b.fired_count for b in batches)
    print("-" * 68)
    print(f"总批次 {len(batches)}，总触发 {total}，单批最大 {max(b.fired_count for b in batches)}")
    print(f"对比修复前：唤醒瞬间一次性触发 {n}，修复后任意时刻 <= 200")


def run_drop_demo():
    print()
    print("=" * 68)
    print("场景：休眠 1 小时，过期策略 drop（只补跑最近 10 秒，更早的丢弃并标记）")
    print("=" * 68)
    clock = ManualClock(start=1_700_000_000.0)
    wheel = TimingWheel(
        tick_ms=TICK_MS, now=clock, max_fires_per_advance=100,
        late_policy=DROP, max_late_ms=10_000,
        on_event=lambda name, data: print(f"  [事件] {name}: {data if name != 'drop' else str(data)[:70] + '...'}"),
    )
    for i in range(1000):
        wheel.add(delay_ms=1000 + (i % 3500) * 1000, payload=f"old-{i}")
    for i in range(150):
        wheel.add(delay_ms=3_595_000, payload=f"recent-{i}")

    clock.sleep(3600)
    first = wheel.advance()
    print(f"首批：触发 {first.fired_count}，丢弃并标记 {len(first.dropped)}，剩余积压 {first.remaining_backlog}")


if __name__ == "__main__":
    run_merge_demo()
    run_drop_demo()
