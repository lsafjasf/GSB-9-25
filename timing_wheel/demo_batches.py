"""演示：休眠 1 小时后，5000 个积压任务在修复前后的触发分批对比。

运行：python3 demo_batches.py
"""

from timing_wheel import TimingWheel


class FakeClock:
    def __init__(self, start_ms=1_000_000):
        self.now_ms = start_ms

    def __call__(self):
        return self.now_ms


def scenario(name, max_fires_per_poll, total=5000, sleep_ms=3_600_000):
    clock = FakeClock()
    wheel = TimingWheel(tick_ms=100, wheel_size=512,
                        max_fires_per_poll=max_fires_per_poll, clock=clock)
    for i in range(total):
        wheel.schedule(f"task-{i}", delay_ms=60_000)
    clock.now_ms += sleep_ms

    batches = []
    while True:
        batch = wheel.poll(clock())
        if not batch:
            break
        batches.append(batch)

    sizes = [len(b) for b in batches]
    overdues = [t.overdue_ms for t in batches[0]] if batches else []
    print(f"[{name}] 积压 {total} 个任务，休眠 {sleep_ms // 1000}s")
    print(f"  批次数: {len(batches)}")
    if len(sizes) <= 30:
        print(f"  每批触发数: {sizes}")
    else:
        print(f"  每批触发数: 前5批 {sizes[:5]} ... 末批 {sizes[-1]}")
    print(f"  单批最大触发数: {max(sizes)}")
    print(f"  首批 overdue_ms 范围: {min(overdues)}..{max(overdues)}")
    print()


if __name__ == "__main__":
    scenario("修复前（无上限）", max_fires_per_poll=None)
    scenario("修复后（上限 200/批）", max_fires_per_poll=200)
