"""ratelimit 自测：行为、并发原子性、时钟异常、参数变更、统计。"""

import threading
import unittest

from ratelimit import (
    FakeClock,
    SlidingWindowRateLimiter,
    TokenBucketRateLimiter,
)


def hammer(limiter, threads, calls_per_thread, n=1):
    """用指定并发度轰炸限流器，返回 (放行数, 拒绝数)。"""
    results = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker():
        barrier.wait()  # 所有线程就绪后同时开抢，最大化竞争
        local = 0
        for _ in range(calls_per_thread):
            if limiter.try_acquire(n).allowed:
                local += 1
        with results_lock:
            results.append(local)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    allowed = sum(results)
    return allowed, threads * calls_per_thread - allowed


class TokenBucketBehaviorTest(unittest.TestCase):
    def test_burst_then_reject_with_deterministic_wait(self):
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=5, rate_per_second=1.0, clock=clock)
        for _ in range(5):
            self.assertTrue(tb.try_acquire().allowed)
        d = tb.try_acquire()
        self.assertFalse(d.allowed)
        self.assertAlmostEqual(d.wait_seconds, 1.0)  # 差 1 个令牌，速率 1/s

    def test_refill_over_time(self):
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=5, rate_per_second=2.0, clock=clock)
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(5)), 5)
        self.assertFalse(tb.try_acquire().allowed)
        clock.advance(2.0)  # 补 4 个令牌
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(4)), 4)
        self.assertFalse(tb.try_acquire().allowed)

    def test_zero_rate_wait_is_inf(self):
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=1, rate_per_second=0.0, clock=clock)
        self.assertTrue(tb.try_acquire().allowed)
        self.assertEqual(tb.try_acquire().wait_seconds, float("inf"))


class SlidingWindowBehaviorTest(unittest.TestCase):
    def test_window_count_and_wait(self):
        clock = FakeClock()
        sw = SlidingWindowRateLimiter(max_requests=3, window_seconds=10.0, clock=clock)
        for _ in range(3):
            self.assertTrue(sw.try_acquire().allowed)
        d = sw.try_acquire()
        self.assertFalse(d.allowed)
        self.assertAlmostEqual(d.wait_seconds, 10.0)  # 等最早一条滑出
        clock.advance(9.0)
        self.assertFalse(sw.try_acquire().allowed)  # 还差 1 秒
        clock.advance(1.0 + 1e-9)
        self.assertTrue(sw.try_acquire().allowed)

    def test_window_slides_gradually(self):
        clock = FakeClock()
        sw = SlidingWindowRateLimiter(max_requests=2, window_seconds=10.0, clock=clock)
        self.assertTrue(sw.try_acquire().allowed)   # t=1000
        clock.advance(6.0)
        self.assertTrue(sw.try_acquire().allowed)   # t=1006
        self.assertFalse(sw.try_acquire().allowed)
        clock.advance(4.0 + 1e-9)                   # t=1010，第一条滑出
        self.assertTrue(sw.try_acquire().allowed)
        self.assertFalse(sw.try_acquire().allowed)  # 第二条还没滑出


class BehaviorContrastTest(unittest.TestCase):
    """两算法行为差异对照：同一流量序列下的不同表现。"""

    def test_burst_difference(self):
        """令牌桶空闲后可一次性突发 capacity 个；滑动窗口在任意窗口内严格不超配额。"""
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=4, rate_per_second=1.0, clock=clock)
        sw = SlidingWindowRateLimiter(max_requests=4, window_seconds=4.0, clock=clock)
        clock.advance(100.0)  # 都空闲很久：桶已积满，窗口已清空
        tb_burst = sum(tb.try_acquire().allowed for _ in range(4))
        sw_burst = sum(sw.try_acquire().allowed for _ in range(4))
        self.assertEqual(tb_burst, 4)
        self.assertEqual(sw_burst, 4)
        # 关键差异：随后 1 秒内令牌桶又补了 1 个令牌可再放行，
        # 滑动窗口必须等最早的记录滑出 4 秒窗口。
        clock.advance(1.0)
        self.assertTrue(tb.try_acquire().allowed)
        self.assertFalse(sw.try_acquire().allowed)

    def test_long_term_rate_converges(self):
        """长期看两者平均速率一致：100 秒内放行数都约等于 配额*时长。"""
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=10, rate_per_second=5.0, clock=clock)
        sw = SlidingWindowRateLimiter(max_requests=5, window_seconds=1.0, clock=clock)
        tb_ok = sw_ok = 0
        for _ in range(1000):  # 每 0.1s 试一次，共 100s
            clock.advance(0.1)
            tb_ok += tb.try_acquire().allowed
            sw_ok += sw.try_acquire().allowed
        # 令牌桶：初始满桶 10 + 100s*5/s = 510（容忍浮点累计误差）
        self.assertLessEqual(abs(tb_ok - 510), 1)
        # 滑动窗口：每 1s 窗口最多 5 个，100s 约 500（无初始突发奖励）
        self.assertEqual(sw_ok, 500)


class ConcurrencyQuotaTest(unittest.TestCase):
    """并发原子性：任意并发度下，单位时间内放行总数不超过配置配额。"""

    THREAD_COUNTS = [1, 2, 4, 8, 16, 32, 64]

    def test_token_bucket_exact_quota_frozen_clock(self):
        """冻结时钟 + 零速率：放行数必须精确等于 capacity，一个都不能多。"""
        for threads in self.THREAD_COUNTS:
            with self.subTest(threads=threads):
                clock = FakeClock()
                tb = TokenBucketRateLimiter(capacity=100, rate_per_second=0.0, clock=clock)
                allowed, rejected = hammer(tb, threads, calls_per_thread=100)
                self.assertEqual(allowed, 100)            # 精确断言：不超发
                self.assertEqual(rejected, threads * 100 - 100)
                s = tb.stats()
                self.assertEqual(s.allowed, 100)
                self.assertEqual(s.rejected, rejected)

    def test_sliding_window_exact_quota_frozen_clock(self):
        """冻结时钟：窗口内放行数必须精确等于 max_requests。"""
        for threads in self.THREAD_COUNTS:
            with self.subTest(threads=threads):
                clock = FakeClock()
                sw = SlidingWindowRateLimiter(max_requests=100, window_seconds=60.0, clock=clock)
                allowed, rejected = hammer(sw, threads, calls_per_thread=100)
                self.assertEqual(allowed, 100)
                self.assertEqual(rejected, threads * 100 - 100)

    def test_token_bucket_quota_with_real_clock_and_refill(self):
        """真实时钟 + 持续补充：放行总数 <= capacity + rate * 实际经过时间。"""
        class RecordingClock:
            def __init__(self):
                import time
                self._mono = time.monotonic
                self.lock = threading.Lock()
                self.first = None
                self.last = None

            def now(self):
                t = self._mono()
                with self.lock:
                    if self.first is None:
                        self.first = t
                    self.last = t
                return t

        clock = RecordingClock()
        capacity, rate = 50, 200.0
        tb = TokenBucketRateLimiter(capacity=capacity, rate_per_second=rate, clock=clock)
        allowed, _ = hammer(tb, threads=32, calls_per_thread=200)
        with clock.lock:
            elapsed = clock.last - clock.first
        upper = capacity + rate * elapsed + 1e-6
        self.assertLessEqual(allowed, upper)  # 精确上界：初始配额 + 期间补充量
        self.assertGreater(allowed, capacity)  # 确实发生了补充，上界不是虚的

    def test_batch_acquire_atomic(self):
        """批量获取也是原子的：n=3 时放行次数 * 3 不超过配额。"""
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=9, rate_per_second=0.0, clock=clock)
        allowed, _ = hammer(tb, threads=16, calls_per_thread=10, n=3)
        self.assertEqual(allowed, 3)  # 3 次 * 3 令牌 = 9，精确打满


class ClockAnomalyTest(unittest.TestCase):
    def test_backwards_clock_does_not_double_grant_token_bucket(self):
        clock = FakeClock(start=1000.0)
        tb = TokenBucketRateLimiter(capacity=5, rate_per_second=10.0, clock=clock)
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(5)), 5)
        clock.set(0.0)  # 大幅回拨 1000 秒
        # 回拨后不得补发：仍然一个都拿不到
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(10)), 0)
        clock.advance(0.3)  # 从回拨后的时间起正常补充：0.3s*10/s=3 个
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(10)), 3)
        self.assertGreaterEqual(tb.stats().clock_backwards, 1)

    def test_backwards_clock_does_not_double_grant_sliding_window(self):
        clock = FakeClock(start=1000.0)
        sw = SlidingWindowRateLimiter(max_requests=3, window_seconds=10.0, clock=clock)
        self.assertEqual(sum(sw.try_acquire().allowed for _ in range(3)), 3)
        clock.set(500.0)  # 回拨 500 秒
        # 回拨不得让旧记录被当作"已过期"而重复发放
        self.assertEqual(sum(sw.try_acquire().allowed for _ in range(10)), 0)
        self.assertGreaterEqual(sw.stats().clock_backwards, 1)

    def test_forward_jump_no_overgrant_no_stall_token_bucket(self):
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=5, rate_per_second=1.0, clock=clock)
        clock.advance(1_000_000.0)  # 向前跳约 11.6 天
        # 跳跃后立即可用（不卡死），但补充量被 capacity 封顶（不超发）
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(10)), 5)

    def test_forward_jump_no_stall_sliding_window(self):
        clock = FakeClock()
        sw = SlidingWindowRateLimiter(max_requests=3, window_seconds=10.0, clock=clock)
        self.assertEqual(sum(sw.try_acquire().allowed for _ in range(3)), 3)
        clock.advance(1_000_000.0)  # 跳跃后旧记录全部过期，立即恢复全配额
        self.assertEqual(sum(sw.try_acquire().allowed for _ in range(3)), 3)
        self.assertFalse(sw.try_acquire().allowed)


class ParamChangeTest(unittest.TestCase):
    def test_capacity_increase_takes_effect_immediately(self):
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=2, rate_per_second=1.0, clock=clock)
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(5)), 2)
        clock.advance(10.0)  # 旧容量下补充被封顶在 2
        tb.set_capacity(5)   # 不铸造令牌：此刻水位仍是 2
        self.assertAlmostEqual(tb.stats().level, 2.0)
        clock.advance(3.0)   # 新容量立即生效：补充上限变为 5
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(10)), 5)

    def test_capacity_decrease_truncates_but_keeps_time_state(self):
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=10, rate_per_second=1.0, clock=clock)
        tb.set_capacity(4)  # 10 个令牌截断到 4
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(10)), 4)
        # 时间状态保留：last_time 未重置，前进 2s 只补 2 个
        clock.advance(2.0)
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(10)), 2)

    def test_rate_change_settles_old_rate_first(self):
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=10, rate_per_second=1.0, clock=clock)
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(10)), 10)
        clock.advance(2.0)   # 旧速率下过了 2s
        tb.set_rate(5.0)     # 变更时先按旧速率结算：+2 个
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(10)), 2)
        clock.advance(1.0)   # 新速率 5/s
        self.assertEqual(sum(tb.try_acquire().allowed for _ in range(10)), 5)

    def test_window_change_re_evicts_immediately(self):
        clock = FakeClock()
        sw = SlidingWindowRateLimiter(max_requests=2, window_seconds=100.0, clock=clock)
        self.assertTrue(sw.try_acquire().allowed)
        self.assertTrue(sw.try_acquire().allowed)
        self.assertFalse(sw.try_acquire().allowed)
        clock.advance(5.0)   # 两条记录已存在 5 秒
        sw.set_window(1.0)   # 窗口骤缩：旧记录立即按新窗口过期
        self.assertTrue(sw.try_acquire().allowed)

    def test_max_requests_change_immediate(self):
        clock = FakeClock()
        sw = SlidingWindowRateLimiter(max_requests=1, window_seconds=60.0, clock=clock)
        self.assertTrue(sw.try_acquire().allowed)
        self.assertFalse(sw.try_acquire().allowed)
        sw.set_max_requests(3)  # 时间没动，立即多 2 个名额
        self.assertTrue(sw.try_acquire().allowed)
        self.assertTrue(sw.try_acquire().allowed)
        self.assertFalse(sw.try_acquire().allowed)


class StatsTest(unittest.TestCase):
    def test_stats_output(self):
        clock = FakeClock()
        tb = TokenBucketRateLimiter(capacity=2, rate_per_second=1.0, clock=clock)
        tb.try_acquire()                    # 放行
        tb.try_acquire()                    # 放行
        tb.try_acquire()                    # 拒绝，wait=1.0
        clock.advance(-5.0)                 # 回拨
        tb.try_acquire()                    # 拒绝，wait=1.0
        s = tb.stats()
        self.assertEqual(s.allowed, 2)
        self.assertEqual(s.rejected, 2)
        self.assertAlmostEqual(s.level, 0.0)          # 水位：令牌已打空
        self.assertAlmostEqual(s.avg_wait_seconds, 0.5)  # (0+0+1+1)/4
        self.assertEqual(s.clock_backwards, 1)

    def test_sliding_window_level(self):
        clock = FakeClock()
        sw = SlidingWindowRateLimiter(max_requests=5, window_seconds=10.0, clock=clock)
        sw.try_acquire()
        sw.try_acquire()
        self.assertEqual(sw.stats().level, 2.0)  # 水位：窗口内已用 2


if __name__ == "__main__":
    unittest.main()
