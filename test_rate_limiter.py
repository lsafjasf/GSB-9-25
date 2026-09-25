"""rate_limiter 自测：python3 test_rate_limiter.py -v"""

import threading
import time
import unittest

from rate_limiter import SlidingWindowLimiter, TokenBucketLimiter


class FakeClock:
    """可手动推进/回拨的注入时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def run_concurrent(limiter, threads: int, attempts_per_thread: int) -> int:
    """所有线程在同一道栅栏后同时冲击 limiter，返回放行总数。"""
    barrier = threading.Barrier(threads)
    allowed = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal allowed
        barrier.wait(timeout=10)
        local = 0
        for _ in range(attempts_per_thread):
            if limiter.allow().allowed:
                local += 1
        with lock:
            allowed += local

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    return allowed


class TokenBucketBasicTest(unittest.TestCase):
    def test_burst_up_to_capacity_then_reject(self):
        clock = FakeClock()
        b = TokenBucketLimiter(capacity=5, refill_rate=1, clock=clock)
        for _ in range(5):
            self.assertTrue(b.allow().allowed)
        d = b.allow()
        self.assertFalse(d.allowed)
        self.assertAlmostEqual(d.wait, 1.0)  # 差 1 个令牌，速率 1/s

    def test_refill_over_time(self):
        clock = FakeClock()
        b = TokenBucketLimiter(capacity=4, refill_rate=2, clock=clock)
        for _ in range(4):
            b.allow()
        clock.advance(1.5)  # 补充 3 个
        self.assertEqual(sum(b.allow().allowed for _ in range(4)), 3)

    def test_initial_bucket_is_full(self):
        clock = FakeClock()
        b = TokenBucketLimiter(capacity=3, refill_rate=1, clock=clock)
        self.assertEqual(sum(b.allow().allowed for _ in range(3)), 3)


class SlidingWindowBasicTest(unittest.TestCase):
    def test_limit_then_reject_then_release_after_window(self):
        clock = FakeClock()
        w = SlidingWindowLimiter(limit=3, window=10, clock=clock)
        for _ in range(3):
            self.assertTrue(w.allow().allowed)
        d = w.allow()
        self.assertFalse(d.allowed)
        self.assertAlmostEqual(d.wait, 10.0)  # 等最旧事件滑出
        clock.advance(10.0)
        self.assertTrue(w.allow().allowed)

    def test_slots_release_individually(self):
        clock = FakeClock()
        w = SlidingWindowLimiter(limit=2, window=10, clock=clock)
        w.allow()
        clock.advance(3)
        w.allow()
        clock.advance(7)  # 第一个事件过期，第二个还剩 3s
        self.assertTrue(w.allow().allowed)
        d = w.allow()
        self.assertFalse(d.allowed)
        self.assertAlmostEqual(d.wait, 3.0)


class BehaviorContrastTest(unittest.TestCase):
    """两种算法在相同长期速率（1 次/s）下的行为差异对照。"""

    def test_burst_then_refill_pattern_differs(self):
        c1, c2 = FakeClock(), FakeClock()
        bucket = TokenBucketLimiter(capacity=10, refill_rate=1, clock=c1)
        window = SlidingWindowLimiter(limit=10, window=10, clock=c2)

        # t=0：两者都允许 10 次突发。
        self.assertEqual(sum(bucket.allow().allowed for _ in range(10)), 10)
        self.assertEqual(sum(window.allow().allowed for _ in range(10)), 10)

        # t=5：令牌桶匀速补充了 5 个令牌 -> 再放行 5 次；
        #       滑动窗口必须等首批事件满 10s -> 放行 0 次。
        c1.advance(5)
        c2.advance(5)
        self.assertEqual(sum(bucket.allow().allowed for _ in range(10)), 5)
        self.assertEqual(sum(window.allow().allowed for _ in range(10)), 0)

        # 建议等待时长也不同：桶只需等 1s 拿下一个令牌，
        # 窗口要等最旧事件满一个完整窗口（还剩 5s）。
        self.assertAlmostEqual(bucket.allow().wait, 1.0)
        self.assertAlmostEqual(window.allow().wait, 5.0)


class ConcurrencyQuotaTest(unittest.TestCase):
    def test_token_bucket_frozen_clock_exact_quota(self):
        """冻结时钟下无补充，任意并发度放行总数必须精确等于容量。"""
        for threads in (1, 8, 32, 64):
            b = TokenBucketLimiter(capacity=100, refill_rate=1000, clock=FakeClock())
            allowed = run_concurrent(b, threads, attempts_per_thread=200)
            self.assertEqual(allowed, 100, f"threads={threads}")
            s = b.stats()
            self.assertEqual(s.allowed, 100)
            self.assertEqual(s.rejected, threads * 200 - 100)

    def test_sliding_window_frozen_clock_exact_quota(self):
        """冻结时钟下窗口不滑动，放行总数必须精确等于 limit。"""
        for threads in (1, 8, 32, 64):
            w = SlidingWindowLimiter(limit=100, window=60, clock=FakeClock())
            allowed = run_concurrent(w, threads, attempts_per_thread=200)
            self.assertEqual(allowed, 100, f"threads={threads}")

    def test_token_bucket_real_clock_never_exceeds_quota(self):
        """真实时钟 + 高并发：放行总数 <= 容量 + 速率 * 实际流逝时间。"""
        capacity, rate = 50.0, 200.0
        b = TokenBucketLimiter(capacity=capacity, refill_rate=rate)
        start = time.monotonic()
        threads, attempts = 32, 200
        barrier = threading.Barrier(threads)
        counts = [0] * threads

        def worker(i: int) -> None:
            barrier.wait(timeout=10)
            c = 0
            for _ in range(attempts):
                if b.allow().allowed:
                    c += 1
            counts[i] = c

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        elapsed = time.monotonic() - start
        total = sum(counts)
        # 精确上界断言：超发即失败（留 1e-6 浮点余量）。
        self.assertLessEqual(total, capacity + rate * elapsed + 1e-6)
        #  sanity：高并发冲击下确实发生了放行与拒绝。
        self.assertGreater(total, 0)
        self.assertGreater(b.stats().rejected, 0)


class ClockAnomalyTest(unittest.TestCase):
    def test_backwards_clock_no_double_grant_bucket(self):
        clock = FakeClock()
        b = TokenBucketLimiter(capacity=10, refill_rate=1, clock=clock)
        for _ in range(10):
            b.allow()
        clock.advance(5)  # 流逝 5s
        self.assertTrue(b.allow().allowed)  # 触发结算：last=1005，剩 4 个
        clock.advance(-60)  # 时钟大幅回拨
        # 回拨既不补充也不回收：恰好剩 4 个可用。
        self.assertEqual(sum(b.allow().allowed for _ in range(10)), 4)
        # 回拨区间不被计入补充：再回拨、再前进，配额不重复发放。
        clock.advance(-1000)
        clock.advance(1000)  # 回到回拨后的同一逻辑时刻
        self.assertFalse(b.allow().allowed)
        clock.advance(61)  # 真实时间越过 last 1s，正常补充 1 个
        self.assertTrue(b.allow().allowed)

    def test_backwards_clock_no_double_grant_window(self):
        clock = FakeClock()
        w = SlidingWindowLimiter(limit=3, window=10, clock=clock)
        for _ in range(3):
            w.allow()
        clock.advance(9)
        clock.advance(-50)  # 回拨：旧事件不得被当作已过期
        self.assertFalse(w.allow().allowed)
        clock.advance(50 + 1)  # 真正满 10s 后才释放
        self.assertTrue(w.allow().allowed)

    def test_forward_jump_no_stall_bucket(self):
        clock = FakeClock()
        b = TokenBucketLimiter(capacity=4, refill_rate=1, clock=clock)
        for _ in range(4):
            b.allow()
        clock.advance(10_000_000)  # 巨幅前跳：补满即止，不累积超额
        self.assertEqual(sum(b.allow().allowed for _ in range(10)), 4)
        d = b.allow()
        self.assertFalse(d.allowed)
        self.assertLessEqual(d.wait, 4.0)  # 等待时长有界，不卡死

    def test_forward_jump_no_stall_window(self):
        clock = FakeClock()
        w = SlidingWindowLimiter(limit=2, window=10, clock=clock)
        w.allow()
        w.allow()
        clock.advance(10_000_000)  # 前跳后窗口立即清空恢复服务
        self.assertTrue(w.allow().allowed)
        self.assertTrue(w.allow().allowed)
        d = w.allow()
        self.assertFalse(d.allowed)
        self.assertLessEqual(d.wait, 10.0)


class ParamChangeTest(unittest.TestCase):
    def test_bucket_capacity_shrink_clamps_level_immediately(self):
        clock = FakeClock()
        b = TokenBucketLimiter(capacity=10, refill_rate=1, clock=clock)
        b.set_params(capacity=4)
        # 水位立即被截到 4：只能再放行 4 次。
        self.assertEqual(sum(b.allow().allowed for _ in range(10)), 4)

    def test_bucket_rate_change_effective_immediately(self):
        clock = FakeClock()
        b = TokenBucketLimiter(capacity=2, refill_rate=1, clock=clock)
        b.allow()
        b.allow()
        self.assertAlmostEqual(b.allow().wait, 1.0)  # 旧速率 1/s
        b.set_params(refill_rate=10)
        self.assertAlmostEqual(b.allow().wait, 0.1)  # 新速率立即生效
        clock.advance(0.1)
        self.assertTrue(b.allow().allowed)

    def test_bucket_time_state_preserved_across_param_change(self):
        clock = FakeClock()
        b = TokenBucketLimiter(capacity=10, refill_rate=2, clock=clock)
        for _ in range(10):
            b.allow()
        clock.advance(2)  # 累积 4 个令牌
        b.set_params(capacity=100, refill_rate=5)  # 变更不重置时间状态
        self.assertEqual(sum(b.allow().allowed for _ in range(10)), 4)

    def test_window_change_effective_immediately(self):
        clock = FakeClock()
        w = SlidingWindowLimiter(limit=2, window=100, clock=clock)
        w.allow()
        w.allow()
        clock.advance(50)  # 事件已发生 50s，仍在 100s 窗口内
        self.assertFalse(w.allow().allowed)
        w.set_params(window=10)  # 窗口缩短，旧事件立即按新窗口淘汰
        self.assertTrue(w.allow().allowed)

    def test_limit_change_effective_immediately(self):
        clock = FakeClock()
        w = SlidingWindowLimiter(limit=2, window=60, clock=clock)
        w.allow()
        w.allow()
        self.assertFalse(w.allow().allowed)
        w.set_params(limit=3)
        self.assertTrue(w.allow().allowed)
        self.assertFalse(w.allow().allowed)
        w.set_params(limit=1)  # 收紧后已有事件保留，但不再放新请求
        self.assertFalse(w.allow().allowed)


class StatsTest(unittest.TestCase):
    def test_stats_output(self):
        clock = FakeClock()
        b = TokenBucketLimiter(capacity=2, refill_rate=1, clock=clock)
        b.allow()
        b.allow()
        b.allow()  # 拒绝，wait=1
        clock.advance(1)
        b.allow()  # 放行
        s = b.stats()
        self.assertEqual(s.allowed, 3)
        self.assertEqual(s.rejected, 1)
        self.assertAlmostEqual(s.level, 0.0)
        self.assertAlmostEqual(s.average_wait, 0.25)  # (0+0+1+0)/4

    def test_window_stats_level(self):
        clock = FakeClock()
        w = SlidingWindowLimiter(limit=5, window=10, clock=clock)
        w.allow()
        w.allow()
        w.allow()
        s = w.stats()
        self.assertEqual((s.allowed, s.rejected, s.level), (3, 0, 3.0))


if __name__ == "__main__":
    unittest.main()
