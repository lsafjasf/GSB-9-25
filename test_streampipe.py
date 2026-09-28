"""streampipe 自测：背压/内存上界、错误传播、关闭语义、统计。"""

import threading
import time
import unittest

from streampipe import (
    SKIP,
    BackpressureTimeout,
    ClosedError,
    Pipeline,
    PipelineError,
    PutCancelled,
)


def slow_consumer(results_iter, delay=0.001):
    out = []
    for item in results_iter:
        out.append(item)
        time.sleep(delay)
    return out


class TestMemoryBound(unittest.TestCase):
    """连续写入远多于缓冲容量的数据，峰值缓冲量不得超过配置上界。"""

    def test_memory_bound_under_fast_producer_slow_consumer(self):
        capacity = 4
        n_stages = 3
        n_items = 5000  # 远多于 (n_stages+1)*capacity = 16

        def stage(x):
            time.sleep(0.0002)  # 每阶段都慢，迫使缓冲承压
            return x + 1

        pipe = Pipeline([stage] * n_stages, capacity=capacity, policy="block")

        def producer():
            for i in range(n_items):
                pipe.put(i)
            pipe.close()

        t = threading.Thread(target=producer)
        t.start()
        collected = slow_consumer(pipe.results(), delay=0.0005)
        t.join()
        self.assertTrue(pipe.wait(timeout=10))

        # 数据完整性与顺序
        self.assertEqual(collected, [i + n_stages for i in range(n_items)])

        stats = pipe.stats()
        bound = (n_stages + 1) * capacity
        # 1) 每个缓冲的高水位不超过其容量
        for peak in stats.per_buffer_peak:
            self.assertLessEqual(peak, capacity)
        # 2) 全局在途（驻留缓冲）峰值不超过理论界 B*C
        self.assertLessEqual(stats.peak_buffered, bound)
        # 3) 背压确实生效过（缓冲真的被填满过，否则测试无意义）
        self.assertGreater(stats.peak_buffered, 0)
        self.assertEqual(stats.dropped, 0)
        self.assertEqual(stats.failed, 0)

    def test_drop_oldest_never_exceeds_capacity(self):
        capacity = 2
        pipe = Pipeline([lambda x: (time.sleep(0.01), x)[1]],
 capacity=capacity,
                        policy="drop_oldest")
        for i in range(200):  # 生产者远快于消费者，但 put 不阻塞
            pipe.put(i)
        pipe.close()
        list(pipe.results())
        self.assertTrue(pipe.wait(timeout=10))
        stats = pipe.stats()
        for peak in stats.per_buffer_peak:
            self.assertLessEqual(peak, capacity)
        self.assertGreater(stats.dropped, 0)  # 确实有丢弃发生
        self.assertLessEqual(stats.peak_buffered, 2 * capacity)


class TestBackpressureBlocking(unittest.TestCase):
    def test_put_blocks_until_consumer_drains(self):
        pipe = Pipeline([lambda x: (time.sleep(0.5), x)[1]], capacity=1)
        pipe.put(1)
        time.sleep(0.01)  # 让阶段线程取走 1，缓冲腾空
        pipe.put(2)
        done = threading.Event()

        def third_put():
            pipe.put(3)  # 缓冲满 -> 阻塞
            done.set()

        t = threading.Thread(target=third_put)
        t.start()
        self.assertFalse(done.wait(0.2), "缓冲满时 put 应当阻塞（背压）")
        it = pipe.results()
        self.assertEqual(next(it), 1)  # 消费者取走一个，背压解除
        self.assertTrue(done.wait(2), "消费者腾出空间后 put 应当成功")
        pipe.close()
        self.assertEqual(list(it), [2, 3])
        t.join()

    def test_put_timeout_under_backpressure(self):
        pipe = Pipeline([lambda x: (time.sleep(0.3), x)[1]], capacity=1)
        pipe.put("a")
        time.sleep(0.01)
        pipe.put("b")  # 填满输入缓冲
        with self.assertRaises(BackpressureTimeout):
            pipe.put("c", timeout=0.1)
        pipe.abort()
        pipe.wait(timeout=5)

    def test_put_cancelled_during_backpressure(self):
        """生产者在背压阻塞期间被取消。"""
        pipe = Pipeline([lambda x: (time.sleep(0.3), x)[1]], capacity=1)
        pipe.put("a")
        time.sleep(0.01)
        pipe.put("b")  # 缓冲已满

        cancel = threading.Event()
        outcome = {}

        def cancelled_producer():
            try:
                pipe.put("c", cancel_event=cancel)
                outcome["result"] = "put-succeeded"
            except PutCancelled:
                outcome["result"] = "cancelled"
            except ClosedError:
                outcome["result"] = "closed"

        t = threading.Thread(target=cancelled_producer)
        t.start()
        time.sleep(0.05)  # 确认对方已阻塞在背压上
        self.assertTrue(t.is_alive())
        cancel.set()
        t.join(timeout=5)
        self.assertEqual(outcome["result"], "cancelled")

        pipe.abort()
        stats = pipe.stats()
        # 取消不算写入也不算失败；中止时缓冲中的数据被明确丢弃
        self.assertEqual(stats.written, 2)
        self.assertEqual(stats.failed, 0)
        self.assertGreaterEqual(stats.dropped, 1)


class TestErrorPropagation(unittest.TestCase):
    def test_stage_error_terminates_pipeline_and_propagates(self):
        def boom(x):
            if x == 5:
                raise ValueError("stage exploded")
            return x

        pipe = Pipeline([lambda x: x, boom, lambda x: x], capacity=4)
        put_errors = []

        def producer():
            try:
                for i in range(1000):
                    pipe.put(i)
            except ClosedError as e:
                put_errors.append(e)

        t = threading.Thread(target=producer)
        t.start()

        received = []
        with self.assertRaises(ValueError) as ctx:
            for item in pipe.results():
                received.append(item)
        # 原始异常原样抛出，类型与消息直接可见，不再包一层 PipelineError
        self.assertNotIsInstance(ctx.exception, PipelineError)
        self.assertIn("exploded", str(ctx.exception))

        t.join(timeout=5)
        self.assertTrue(pipe.wait(timeout=5))
        # 生产者随后写入失败
        self.assertTrue(put_errors, "失败后生产者 put 应抛 ClosedError")
        with self.assertRaises(ClosedError):
            pipe.put(999)

        stats = pipe.stats()
        self.assertEqual(stats.failed, 1)
        # 失败时已缓冲未交付的数据被明确丢弃并计数
        self.assertGreater(stats.dropped, 0)
        # 一致性：已交付 + 已丢弃 + 已过滤 <= 已写入；不会有数据凭空消失
        self.assertLessEqual(len(received) + stats.dropped, stats.written)

    def test_first_stage_error_still_propagates(self):
        pipe = Pipeline([lambda x: 1 / 0], capacity=2)
        pipe.put(1)
        pipe.close()
        with self.assertRaises(ZeroDivisionError):
            list(pipe.results())
        self.assertEqual(pipe.stats().failed, 1)


class TestCloseSemantics(unittest.TestCase):
    def test_graceful_close_drains_remaining(self):
        """上游提前关闭：消费者仍能读到全部剩余数据。"""
        pipe = Pipeline([lambda x: x * 2, lambda x: x + 1], capacity=4)
        for i in range(3):
            pipe.put(i)
        pipe.close()
        self.assertEqual(slow_consumer(pipe.results()), [1, 3, 5])
        self.assertTrue(pipe.wait(timeout=5))
        stats = pipe.stats()
        self.assertEqual(stats.written, 3)
        self.assertEqual(stats.processed, 6)  # 3 个元素 * 2 个阶段
        self.assertEqual(stats.dropped, 0)
        self.assertEqual(stats.failed, 0)

    def test_close_is_idempotent_and_put_after_close_fails(self):
        pipe = Pipeline([lambda x: x], capacity=2)
        pipe.put(1)
        self.assertTrue(pipe.close())
        self.assertFalse(pipe.close())  # 重复关闭安全
        pipe.close()
        with self.assertRaises(ClosedError):
            pipe.put(2)
        self.assertEqual(list(pipe.results()), [1])

    def test_abort_is_idempotent(self):
        pipe = Pipeline([lambda x: (time.sleep(0.05), x)[1]], capacity=2)
        pipe.put(1)
        self.assertTrue(pipe.abort())
        self.assertFalse(pipe.abort())
        with self.assertRaises(ClosedError):
            pipe.put(2)
        with self.assertRaises(PipelineError):
            list(pipe.results())

    def test_consumer_exception_aborts_via_context_manager(self):
        """消费者异常：通过 with 语句中止管道，生产者写入失败。"""
        pipe = Pipeline([lambda x: x], capacity=2)
        producer_outcome = []

        def producer():
            try:
                for i in range(10000):
                    pipe.put(i)
                    time.sleep(0.0001)
            except ClosedError:
                producer_outcome.append("closed")

        t = threading.Thread(target=producer)
        t.start()
        consumed = 0
        with self.assertRaises(RuntimeError):
            with pipe:
                for _ in pipe.results():
                    consumed += 1
                    if consumed == 3:
                        raise RuntimeError("consumer blew up")
        t.join(timeout=5)
        self.assertEqual(producer_outcome, ["closed"])
        stats = pipe.stats()
        self.assertTrue(stats.aborted)
        self.assertGreaterEqual(stats.dropped, 0)


class TestMinimalRepros(unittest.TestCase):
    """两个缺陷的最小复现：放弃迭代 / 异常类型被统一包装。"""

    def test_abandoned_iteration_lets_all_threads_exit(self):
        # 消费者拿到少量结果后直接关掉生成器（不走 with、不调 abort/close）。
        # capacity=1：所有缓冲都会被填满，生产者与全部阶段线程必然卡死。
        pipe = Pipeline([lambda x: x, lambda x: x], capacity=1)

        producer_outcome = []

        def producer():
            try:
                for i in range(100000):
                    pipe.put(i)  # 修复前：永远阻塞在这里
            except ClosedError:
                producer_outcome.append("closed")  # 中止解除阻塞后收到关闭信号

        t_prod = threading.Thread(target=producer, daemon=True)
        t_prod.start()

        it = pipe.results()
        self.assertEqual(next(it), 0)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if all(not t.is_alive() for t in pipe._threads):
                break
            time.sleep(0.01)
        # 先证明死锁前提成立：两个阶段线程都还活着（全卡在满缓冲上）
        self.assertTrue(all(t.is_alive() for t in pipe._threads))
        self.assertTrue(t_prod.is_alive())
        self.assertEqual(len(pipe._buffers[-1]), 1)

        it.close()  # 消费者中途放弃迭代

        t_prod.join(timeout=2)
        self.assertFalse(t_prod.is_alive(), "放弃迭代后生产者仍被背压卡死")
        self.assertEqual(producer_outcome, ["closed"])
        self.assertTrue(pipe.wait(timeout=2), "放弃迭代后阶段线程未能退出")
        self.assertTrue(pipe.stats().aborted)

    def test_original_stage_exception_type_is_visible_directly(self):
        class BoomError(RuntimeError):
            pass

        original = BoomError("stage exploded")

        def boom(x):
            raise original

        pipe = Pipeline([boom], capacity=1)
        pipe.put(1)
        with self.assertRaises(BoomError) as ctx:
            list(pipe.results())
        # 原始异常原样可见，调用方无需再翻 __cause__
        self.assertIs(ctx.exception, original)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(pipe.wait(timeout=5))


class TestStatsAndFilter(unittest.TestCase):
    def test_skip_filter_and_stats_consistency(self):
        def keep_even(x):
            return x if x % 2 == 0 else SKIP

        pipe = Pipeline([keep_even], capacity=4)
        for i in range(10):
            pipe.put(i)
        pipe.close()
        self.assertEqual(list(pipe.results()), [0, 2, 4, 6, 8])
        stats = pipe.stats()
        self.assertEqual(stats.written, 10)
        self.assertEqual(stats.dropped, 5)   # 5 个奇数被过滤
        self.assertEqual(stats.processed, 5)
        self.assertEqual(stats.failed, 0)

    def test_drop_newest_returns_false(self):
        pipe = Pipeline([lambda x: (time.sleep(0.05), x)[1]], capacity=1,
                        policy="drop_newest")
        self.assertTrue(pipe.put("a"))
        time.sleep(0.01)
        self.assertTrue(pipe.put("b"))
        rejected = [pipe.put(f"x{i}") for i in range(5)]
        self.assertIn(False, rejected)
        pipe.abort()
        pipe.wait(timeout=5)
        self.assertGreater(pipe.stats().dropped, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
