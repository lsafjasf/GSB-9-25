"""复现「休眠后集中触发」：对修复前的 BuggyTimingWheel 稳定复现。

用 ManualClock.sleep() 模拟机器休眠 1 小时，期间 5000 个任务到期；
唤醒后第一次 advance() 就把 5000 个任务一次性全部返回 —— 这就是打爆
下游的那次集中触发。该用例永远「红」在旧实现上，作为问题存在的证据。
"""

import unittest

from timing_wheel.buggy import BuggyTimingWheel
from timing_wheel.clock import ManualClock

TASKS = 5000
SLEEP_SECONDS = 3600  # 休眠 1 小时
SAFE_LIMIT = 100  # 下游一次能承受的触发上限（与修复版的默认上限一致）


class ReproBurstTest(unittest.TestCase):
    def test_sleep_causes_single_call_burst(self):
        clock = ManualClock(start=1_700_000_000.0)
        wheel = BuggyTimingWheel(tick_ms=100, now=clock)
        for i in range(TASKS):
            wheel.add(delay_ms=1000 + (i % 3_599_000), payload=f"task-{i}")

        clock.sleep(SLEEP_SECONDS)  # 机器休眠，时间瞬间跳过 1 小时
        fired = wheel.advance()  # 唤醒后第一次推进

        # 旧实现：同一时刻（同一次 advance）触发全部积压任务
        self.assertEqual(len(fired), TASKS)
        self.assertGreater(len(fired), SAFE_LIMIT, "单次触发数远超下游承受能力")

    def test_short_sleep_also_bursts(self):
        clock = ManualClock(start=1_700_000_000.0)
        wheel = BuggyTimingWheel(tick_ms=100, now=clock)
        for i in range(300):
            wheel.add(delay_ms=500, payload=f"task-{i}")

        clock.sleep(5)  # 只睡 5 秒也会集中触发
        fired = wheel.advance()

        self.assertEqual(len(fired), 300)


if __name__ == "__main__":
    unittest.main()
