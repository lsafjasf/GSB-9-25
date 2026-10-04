"""时间轮休眠爆发问题的复现与回归测试。

运行：python3 -m unittest test_timing_wheel -v   （在 timing_wheel/ 目录下）
"""

import unittest

from timing_wheel import TimingWheel


class FakeClock:
    """可注入的假时钟，用 advance 模拟机器休眠后的时间跳变。"""

    def __init__(self, start_ms=1_000_000):
        self.now_ms = start_ms

    def __call__(self):
        return self.now_ms

    def advance(self, ms):
        self.now_ms += ms


def drain(wheel, clock):
    """连续 poll 直到没有任务触发，返回每批的触发列表。"""
    batches = []
    while True:
        batch = wheel.poll(clock())
        if not batch:
            break
        batches.append(batch)
    return batches


class BurstReproTest(unittest.TestCase):
    """复现：休眠 1 小时后，无上限的旧行为把积压任务一次性全部触发。"""

    def test_unbounded_poll_fires_everything_at_once(self):
        clock = FakeClock()
        wheel = TimingWheel(tick_ms=100, wheel_size=512,
                            max_fires_per_poll=None, clock=clock)  # 旧行为
        total = 5000
        for i in range(total):
            wheel.schedule(f"task-{i}", delay_ms=60_000)

        clock.advance(3_600_000)  # 机器休眠 1 小时
        fired = wheel.poll(clock())

        self.assertEqual(len(fired), total)  # 一次 poll 打爆下游
        self.assertTrue(all(t.overdue_ms > 0 for t in fired))


class BoundedDrainTest(unittest.TestCase):
    """修复后：同样的长时间休眠，触发被限流分批。"""

    def test_long_sleep_is_drained_in_bounded_batches(self):
        clock = FakeClock()
        limit = 200
        wheel = TimingWheel(tick_ms=100, wheel_size=512,
                            max_fires_per_poll=limit, clock=clock)
        total = 5000
        for i in range(total):
            wheel.schedule(f"task-{i}", delay_ms=60_000)

        clock.advance(3_600_000)  # 休眠 1 小时
        batches = drain(wheel, clock)

        self.assertTrue(batches)
        for batch in batches:
            self.assertLessEqual(len(batch), limit)
        fired = [t for batch in batches for t in batch]
        self.assertEqual(len(fired), total)
        self.assertEqual(len({t.key for t in fired}), total)  # 不丢不重
        self.assertTrue(all(t.overdue_ms > 0 for t in fired))  # 过期被标记
        deadlines = [t.deadline_ms for t in fired]
        self.assertEqual(deadlines, sorted(deadlines))  # 按截止时间顺序出队
        self.assertEqual(wheel.pending, 0)

    def test_large_backlog(self):
        clock = FakeClock()
        limit = 500
        wheel = TimingWheel(tick_ms=100, wheel_size=512,
                            max_fires_per_poll=limit, clock=clock)
        total = 50_000
        for i in range(total):
            wheel.schedule(f"bulk-{i}", delay_ms=30_000 + (i % 10_000))

        clock.advance(7_200_000)  # 休眠 2 小时
        batches = drain(wheel, clock)

        fired = [t for batch in batches for t in batch]
        self.assertEqual(len(fired), total)
        self.assertTrue(all(len(b) <= limit for b in batches))
        self.assertTrue(all(t.overdue_ms > 0 for t in fired))
        self.assertEqual(wheel.pending, 0)


class ShortJumpTest(unittest.TestCase):
    """短时间跳变：只跨过少量 tick，过期量小，标记准确。"""

    def test_short_jump_marks_small_overdue(self):
        clock = FakeClock()
        wheel = TimingWheel(tick_ms=100, wheel_size=512,
                            max_fires_per_poll=10, clock=clock)
        task = wheel.schedule("short", delay_ms=200)

        clock.advance(350)  # 跳变 350ms，错过截止 150ms
        fired = wheel.poll(clock())

        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].key, "short")
        self.assertEqual(fired[0].overdue_ms, 150)
        self.assertEqual(fired[0].deadline_ms, task.deadline_ms)


class NoJumpTest(unittest.TestCase):
    """无跳变：正常走时，任务按时触发且不提前。"""

    def test_normal_pace_fires_on_time(self):
        clock = FakeClock()
        wheel = TimingWheel(tick_ms=100, wheel_size=512,
                            max_fires_per_poll=10, clock=clock)
        wheel.schedule("a", delay_ms=300)
        wheel.schedule("b", delay_ms=1000)

        clock.advance(299)
        self.assertEqual(wheel.poll(clock()), [])  # 不提前
        clock.advance(1)
        fired = wheel.poll(clock())
        self.assertEqual([t.key for t in fired], ["a"])
        self.assertEqual(fired[0].overdue_ms, 0)  # 准时

        clock.advance(700)
        fired = wheel.poll(clock())
        self.assertEqual([t.key for t in fired], ["b"])
        self.assertEqual(fired[0].overdue_ms, 0)

    def test_far_future_task_waits_multiple_rounds(self):
        clock = FakeClock()
        wheel = TimingWheel(tick_ms=100, wheel_size=8,
                            max_fires_per_poll=10, clock=clock)
        wheel.schedule("far", delay_ms=5000)  # 远超一圈（800ms）

        clock.advance(4999)
        self.assertEqual(wheel.poll(clock()), [])
        clock.advance(1)
        fired = wheel.poll(clock())
        self.assertEqual([t.key for t in fired], ["far"])
        self.assertEqual(fired[0].overdue_ms, 0)


class CancelTest(unittest.TestCase):
    def test_cancelled_task_never_fires(self):
        clock = FakeClock()
        wheel = TimingWheel(tick_ms=100, wheel_size=512,
                            max_fires_per_poll=10, clock=clock)
        task = wheel.schedule("gone", delay_ms=100)
        self.assertTrue(wheel.cancel(task))
        self.assertFalse(wheel.cancel(task))

        clock.advance(10_000)
        self.assertEqual(wheel.poll(clock()), [])
        self.assertEqual(wheel.pending, 0)


if __name__ == "__main__":
    unittest.main()
