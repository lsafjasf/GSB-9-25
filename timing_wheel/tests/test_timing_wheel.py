"""修复后 TimingWheel 的回归测试。

覆盖：无跳变（正常走时）、短时间跳变、长时间休眠、大量积压、
时钟不动 / 回拨、以及 drop 策略对过期任务的标记。
"""

import math
import unittest

from timing_wheel.clock import ManualClock
from timing_wheel.wheel import DROP, TimingWheel

TICK_MS = 100
START = 1_700_000_000.0


def make_wheel(clock, max_fires=100, **kw):
    return TimingWheel(tick_ms=TICK_MS, now=clock, max_fires_per_advance=max_fires, **kw)


def drain(wheel, clock, max_ticks=100_000):
    """逐 tick 推进时钟直到积压清空，返回所有批次。"""
    batches = []
    for _ in range(max_ticks):
        clock.tick(TICK_MS / 1000)
        batch = wheel.advance()
        batches.append(batch)
        if wheel.backlog_size == 0:
            break
    return batches


class NoJumpTest(unittest.TestCase):
    """无跳变：正常走时时行为与旧实现一致，且每 tick 触发数不超过上限。"""

    def test_normal_schedule_fires_on_time(self):
        clock = ManualClock(START)
        wheel = make_wheel(clock)
        wheel.add(delay_ms=300, payload="a")
        wheel.add(delay_ms=100, payload="b")

        clock.tick(0.1)
        self.assertEqual(wheel.advance().fired, ["b"])
        clock.tick(0.1)
        self.assertEqual(wheel.advance().fired, [])
        clock.tick(0.1)
        self.assertEqual(wheel.advance().fired, ["a"])
        self.assertEqual(wheel.pending, 0)

    def test_normal_ticks_respect_cap(self):
        clock = ManualClock(START)
        wheel = make_wheel(clock, max_fires=10)
        for i in range(25):
            wheel.add(delay_ms=100, payload=i)

        clock.tick(0.1)
        first = wheel.advance()
        self.assertEqual(first.fired_count, 10)
        self.assertEqual(first.remaining_backlog, 15)

        rest = drain(wheel, clock)
        fired = first.fired + [p for b in rest for p in b.fired]
        self.assertEqual(fired, list(range(25)))  # 全部触发且顺序不乱
        for b in rest:
            self.assertLessEqual(b.fired_count, 10)


class ShortJumpTest(unittest.TestCase):
    """短时间跳变：跨过几个 tick，过期任务合并后受上限约束。"""

    def test_small_jump_under_cap(self):
        clock = ManualClock(START)
        wheel = make_wheel(clock, max_fires=100)
        for i in range(8):
            wheel.add(delay_ms=200, payload=i)

        clock.sleep(0.5)  # 跳 5 个 tick
        batch = wheel.advance()
        self.assertEqual(batch.fired_count, 8)  # 未超上限，一次消化完
        self.assertEqual(batch.skipped_ticks, 4)
        self.assertEqual(batch.remaining_backlog, 0)

    def test_small_jump_over_cap_is_batched(self):
        clock = ManualClock(START)
        wheel = make_wheel(clock, max_fires=100)
        for i in range(250):
            wheel.add(delay_ms=200, payload=i)

        clock.sleep(0.3)  # 跳 3 个 tick，250 个任务过期
        first = wheel.advance()
        self.assertEqual(first.fired_count, 100)  # 硬上限
        self.assertEqual(first.remaining_backlog, 150)

        rest = drain(wheel, clock)
        self.assertEqual([b.fired_count for b in rest], [100, 50])
        fired = first.fired + [p for b in rest for p in b.fired]
        self.assertEqual(fired, list(range(250)))


class LongSleepTest(unittest.TestCase):
    """长时间休眠：合并过期任务，分批限速消化，同一时刻触发数有硬上限。"""

    def test_one_hour_sleep_is_rate_limited(self):
        clock = ManualClock(START)
        wheel = make_wheel(clock, max_fires=200)
        n = 5000
        for i in range(n):
            wheel.add(delay_ms=1000 + (i % 3_599_000), payload=i)

        clock.sleep(3600)  # 休眠 1 小时
        first = wheel.advance()
        self.assertEqual(first.fired_count, 200)  # 唤醒瞬间最多 200 个
        self.assertEqual(first.skipped_ticks, 36000 - 1)
        self.assertEqual(first.remaining_backlog, n - 200)

        rest = drain(wheel, clock)
        for b in rest:
            self.assertLessEqual(b.fired_count, 200)
        fired = first.fired + [p for b in rest for p in b.fired]
        self.assertEqual(sorted(fired), list(range(n)))  # 合并策略：一个都不丢

    def test_backlog_drains_one_cap_per_tick(self):
        clock = ManualClock(START)
        wheel = make_wheel(clock, max_fires=50)
        for i in range(300):
            wheel.add(delay_ms=100, payload=i)

        clock.sleep(3600)
        sizes = [wheel.advance().fired_count]
        while wheel.backlog_size:
            clock.tick(0.1)
            sizes.append(wheel.advance().fired_count)
        self.assertEqual(sizes, [50, 50, 50, 50, 50, 50])  # 每 tick 恰好一批

    def test_clock_jump_event_reported(self):
        events = []
        clock = ManualClock(START)
        wheel = make_wheel(clock, on_event=lambda name, data: events.append((name, data)))
        wheel.add(delay_ms=100, payload="x")
        clock.sleep(10)
        wheel.advance()
        self.assertEqual(events[0][0], "clock_jump")
        self.assertEqual(events[0][1]["skipped_ticks"], 99)


class LargeBacklogTest(unittest.TestCase):
    """大量积压：批次数 = ceil(n / 上限)，每批都不超限。"""

    def test_ten_thousand_tasks_batch_counts(self):
        clock = ManualClock(START)
        wheel = make_wheel(clock, max_fires=250)
        n = 10_000
        for i in range(n):
            wheel.add(delay_ms=100, payload=i)

        clock.sleep(3600)
        batches = [wheel.advance()]
        while wheel.backlog_size:
            clock.tick(0.1)
            batches.append(wheel.advance())

        expected = math.ceil(n / 250)
        self.assertEqual(len(batches), expected)
        self.assertEqual([b.fired_count for b in batches[:-1]], [250] * (expected - 1))
        self.assertEqual(batches[-1].fired_count, n - 250 * (expected - 1))
        self.assertEqual(sum(b.fired_count for b in batches), n)


class FrozenOrBackwardClockTest(unittest.TestCase):
    """时钟不动 / 回拨：不触发、不消化积压、不炸。"""

    def test_no_time_pass_no_fire(self):
        clock = ManualClock(START)
        wheel = make_wheel(clock, max_fires=10)
        for i in range(30):
            wheel.add(delay_ms=100, payload=i)
        clock.sleep(1)
        wheel.advance()  # 积压 20
        self.assertEqual(wheel.backlog_size, 20)

        for _ in range(5):  # 时钟不走，反复调用也不许再触发
            batch = wheel.advance()
            self.assertEqual(batch.fired_count, 0)
        self.assertEqual(wheel.backlog_size, 20)

    def test_backward_clock_is_ignored(self):
        clock = ManualClock(START)
        wheel = make_wheel(clock)
        wheel.add(delay_ms=100, payload="x")
        clock.rewind(5)  # 模拟 NTP 回拨 5 秒
        batch = wheel.advance()
        self.assertEqual(batch.fired_count, 0)
        self.assertEqual(wheel.pending, 1)


class DropPolicyTest(unittest.TestCase):
    """drop 策略：过期太久的任务被丢弃并显式标记，未过期的正常触发。"""

    def test_stale_tasks_dropped_and_marked(self):
        events = []
        clock = ManualClock(START)
        wheel = make_wheel(
            clock,
            late_policy=DROP,
            max_late_ms=10_000,  # 允许补跑最近 10 秒内的任务
            on_event=lambda name, data: events.append((name, data)),
        )
        wheel.add(delay_ms=100, payload="ancient")  # 休眠结束时已过期 ~20s
        wheel.add(delay_ms=15_000, payload="recent")  # 过期 ~5s，仍应补跑
        wheel.add(delay_ms=60_000, payload="future")  # 未到期

        clock.sleep(20)
        batch = wheel.advance()

        self.assertEqual(batch.dropped, ["ancient"])
        self.assertEqual(batch.fired, ["recent"])
        self.assertEqual(wheel.pending, 1)  # future 还在轮上
        self.assertIn(("drop", ["ancient"]), events)


if __name__ == "__main__":
    unittest.main()
