"""pipeline 的自测：背压、内存上界、错误传播、关闭语义、统计。

运行：python3 -m unittest test_pipeline -v
"""

import threading
import time
import unittest

from pipeline import (
    _END,
    SKIP,
    BackpressureTimeout,
    BoundedBuffer,
    ClosedError,
    DropPolicy,
    Pipeline,
    PipelineError,
)


def run_producer(pipe, items, errors, close=True):
    """在后台线程中生产数据，记录写端异常。"""
    def _run():
        try:
            for x in items:
                pipe.put(x)
            if close:
                pipe.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    t = threading.Thread(target=_run, name="test-producer", daemon=True)
    t.start()
    return t


class TestBoundedBuffer(unittest.TestCase):
    """缓冲原语：三种背压策略 + 关闭语义。"""

    def test_block_policy_blocks_and_wakes(self):
        buf = BoundedBuffer(2, DropPolicy.BLOCK)
        buf.put(1)
        buf.put(2)
        done = []

        def putter():
            buf.put(3)
            done.append(True)

        t = threading.Thread(target=putter, daemon=True)
        t.start()
        time.sleep(0.1)
        self.assertEqual(done, [], "缓冲满时 BLOCK 策略必须阻塞生产者")
        self.assertEqual(buf.get(), 1)
        t.join(timeout=2)
        self.assertEqual(done, [True], "消费腾出空间后生产者应被唤醒")
        self.assertEqual(buf.stats.peak, 2)

    def test_put_timeout_under_backpressure(self):
        buf = BoundedBuffer(1, DropPolicy.BLOCK)
        buf.put(1)
        with self.assertRaises(BackpressureTimeout):
            buf.put(2, timeout=0.05)

    def test_drop_newest(self):
        buf = BoundedBuffer(2, DropPolicy.DROP_NEWEST)
        self.assertTrue(buf.put(1))
        self.assertTrue(buf.put(2))
        self.assertFalse(buf.put(3), "满时 DROP_NEWEST 应丢弃新元素")
        self.assertEqual(buf.stats.dropped, 1)
        buf.close()
        self.assertEqual([buf.get(), buf.get()], [1, 2])
        self.assertIs(buf.get(), _END)

    def test_drop_oldest(self):
        buf = BoundedBuffer(2, DropPolicy.DROP_OLDEST)
        buf.put(1)
        buf.put(2)
        buf.put(3)  # 挤出 1
        self.assertEqual(buf.stats.dropped, 1)
        buf.close()
        self.assertEqual([buf.get(), buf.get()], [2, 3])
        self.assertIs(buf.get(), _END)

    def test_close_idempotent_and_put_fails(self):
        buf = BoundedBuffer(2)
        buf.put(1)
        buf.close()
        buf.close()  # 重复关闭安全
        with self.assertRaises(ClosedError):
            buf.put(2)
        self.assertEqual(buf.get(), 1)   # 已有关数据仍可消费
        self.assertIs(buf.get(), _END)

    def test_abort_discards_and_counts(self):
        buf = BoundedBuffer(4)
        buf.put(1)
        buf.put(2)
        buf.abort(RuntimeError("boom"))
        buf.abort(RuntimeError("again"))  # 幂等
        self.assertEqual(buf.stats.dropped, 2)
        with self.assertRaises(RuntimeError):
            buf.get()
        with self.assertRaises(RuntimeError):
            buf.put(3)


class TestPipelineBasics(unittest.TestCase):
    def test_multi_stage_chain_and_filter(self):
        pipe = Pipeline(capacity=4)
        pipe.add_stage(lambda x: x + 1, name="incr")
        pipe.add_stage(lambda x: SKIP if x % 2 else x, name="drop_odd")
        pipe.add_stage(lambda x: x * 10, name="mul10")
        pipe.start()
        for i in range(10):
            pipe.put(i)
        pipe.close()
        # (i+1) 为偶数 -> i 为奇数；结果 ((i+1)*10)
        self.assertEqual(list(pipe), [(i + 1) * 10 for i in range(1, 10, 2)])
        stats = pipe.stats()
        self.assertEqual(stats.written, 10)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.dropped, 0)

    def test_slow_consumer_block_backpressure(self):
        """消费者很慢：BLOCK 策略下生产者被压回，数据不丢不重。"""
        n = 500
        pipe = Pipeline(capacity=4)
        pipe.add_stage(lambda x: x * 2, name="double")
        pipe.start()
        errors = []
        t = run_producer(pipe, range(n), errors)
        got = []
        for item in pipe:
            got.append(item)
            if len(got) % 50 == 0:
                time.sleep(0.002)  # 慢消费者
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(got, [i * 2 for i in range(n)])
        stats = pipe.stats()
        self.assertEqual(stats.written, n)
        self.assertEqual(stats.dropped, 0)


class TestMemoryBound(unittest.TestCase):
    """内存上界证明：写入量 >> 缓冲容量时，峰值缓冲量不超过配置。"""

    def test_peak_buffered_never_exceeds_capacity(self):
        cap = 8
        n = 20_000  # 远多于任何缓冲容量
        pipe = Pipeline(capacity=cap)
        pipe.add_stage(lambda x: x + 1, name="s1")
        pipe.add_stage(lambda x: x * 2, name="s2")
        pipe.add_stage(lambda x: x, name="s3")
        pipe.start()

        # 理论全局上界：sum(capacity) + 每阶段手中 1 个 + 消费者手中 1 个
        global_bound = sum(b.capacity for b in pipe.buffers) \
            + len(pipe.buffers) + 1
        violations = []
        stop = threading.Event()

        def monitor():
            while not stop.is_set():
                total = sum(len(b) for b in pipe.buffers)
                if total > global_bound:
                    violations.append(total)
                time.sleep(0.0002)

        mon = threading.Thread(target=monitor, daemon=True)
        mon.start()

        errors = []
        t = run_producer(pipe, range(n), errors)
        consumed = 0
        for _ in pipe:
            consumed += 1
            if consumed % 200 == 0:
                time.sleep(0.001)  # 慢消费者，制造背压
        t.join(timeout=30)
        stop.set()
        mon.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(consumed, n)
        self.assertEqual(violations, [],
                         f"全局缓冲量越过上界 {global_bound}: {violations[:5]}")
        for buf in pipe.buffers:
            self.assertLessEqual(
                buf.stats.peak, buf.capacity,
                f"缓冲 {buf.name} 峰值 {buf.stats.peak} 超过容量 {buf.capacity}")
        stats = pipe.stats()
        self.assertLessEqual(stats.peak_buffered,
                             sum(b.capacity for b in pipe.buffers))
        print(f"\n[memory-bound] n={n} cap={cap} "
              f"peak_buffered={stats.peak_buffered} "
              f"global_bound={global_bound} monitor_violations=0")


class TestErrorPropagation(unittest.TestCase):
    def test_stage_error_stops_everything(self):
        """某阶段抛错：消费者收到原始异常、阻塞的生产者被唤醒、
        已缓冲数据被丢弃并计数、统计反映失败。"""
        def parse(x):
            if x == 13:
                raise ValueError("bad item 13")
            return x

        pipe = Pipeline(capacity=4)
        pipe.add_stage(parse, name="parse")
        pipe.add_stage(lambda x: x * 2, name="double")
        pipe.start()

        errors = []
        t = run_producer(pipe, range(10_000), errors)

        got = []
        with self.assertRaises(ValueError) as ctx:
            for item in pipe:
                got.append(item)
        self.assertEqual(str(ctx.exception), "bad item 13")

        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "出错后阻塞的生产者必须被唤醒")
        self.assertTrue(errors, "生产者在背压中应收到传播过来的异常")
        self.assertIsInstance(errors[0], ValueError)

        stats = pipe.stats()
        self.assertEqual(stats.failed, 1)
        self.assertGreater(stats.dropped, 0,
                           "失败时已缓冲的数据必须被丢弃并计数，不得静默吞掉")
        self.assertLess(stats.processed, 10_000 * 2)
        print(f"\n[error-propagation] consumed_before_error={len(got)} "
              f"stats: {stats}")

    def test_error_not_silently_swallowed(self):
        """即使消费者只读到 _END，残留错误也要抛出。"""
        pipe = Pipeline(capacity=2)
        def boom(x):
            raise KeyError("always")
        pipe.add_stage(boom, name="boom")
        pipe.start()
        pipe.put(1)
        pipe.close()
        with self.assertRaises(KeyError):
            list(pipe)
        self.assertEqual(pipe.stats().failed, 1)


class TestCloseSemantics(unittest.TestCase):
    def test_upstream_early_close_drains_remaining(self):
        """上游提前关闭：消费者能读到全部剩余数据。"""
        pipe = Pipeline(capacity=4)
        pipe.add_stage(lambda x: x * 2, name="double")
        pipe.start()
        for i in range(5):  # 计划写 100 个，只写 5 个就关闭
            pipe.put(i)
        pipe.close()
        self.assertEqual(list(pipe), [0, 2, 4, 6, 8])
        stats = pipe.stats()
        self.assertEqual(stats.written, 5)
        self.assertEqual(stats.processed, 5)
        self.assertEqual(stats.failed, 0)

    def test_double_close_safe_and_put_after_close_fails(self):
        pipe = Pipeline(capacity=2)
        pipe.add_stage(lambda x: x, name="id")
        pipe.start()
        pipe.put(1)
        pipe.close()
        pipe.close()  # 重复关闭安全
        with self.assertRaises(ClosedError):
            pipe.put(2)
        self.assertEqual(list(pipe), [1])

    def test_abort_idempotent(self):
        pipe = Pipeline(capacity=2)
        pipe.add_stage(lambda x: x, name="id")
        pipe.start()
        pipe.abort(RuntimeError("stop"))
        pipe.abort(RuntimeError("stop again"))  # 幂等，不抛错
        with self.assertRaises(RuntimeError):
            pipe.put(1)
        self.assertTrue(pipe.join(timeout=5))


class TestConsumerAndProducerFailure(unittest.TestCase):
    def test_consumer_exception_aborts_pipeline(self):
        """消费者异常：整条管道中止，阻塞中的生产者被唤醒并收到异常。"""
        pipe = Pipeline(capacity=4)
        pipe.add_stage(lambda x: x, name="id")
        pipe.start()

        errors = []
        t = run_producer(pipe, range(100_000), errors)

        with self.assertRaises(RuntimeError):
            for i, _ in enumerate(pipe):
                if i == 5:
                    raise RuntimeError("consumer boom")
        # 消费者异常 -> 生成器被关闭 -> 自动 abort -> 生产者被唤醒
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "消费者异常后生产者不得挂死")
        self.assertTrue(errors, "生产者应收到管道中止的异常")
        self.assertTrue(pipe.join(timeout=5))
        print(f"\n[consumer-exception] producer_error={type(errors[0]).__name__} "
              f"stats: {pipe.stats()}")

    def test_producer_cancelled_during_backpressure(self):
        """生产者在背压阻塞中被取消（外部 abort）：put 抛错，线程不死锁。"""
        pipe = Pipeline(capacity=4)
        pipe.add_stage(lambda x: x, name="id")
        pipe.start()
        # 不消费，让缓冲填满、生产者阻塞在 put 上
        errors = []
        t = run_producer(pipe, range(100_000), errors)
        time.sleep(0.2)  # 等生产者进入背压阻塞
        self.assertTrue(t.is_alive(), "生产者应处于背压阻塞中")
        pipe.abort(PipelineError("cancelled"))  # 取消
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "被取消的生产者必须退出，不得死锁")
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], PipelineError)
        stats = pipe.stats()
        self.assertGreater(stats.dropped, 0, "abort 时缓冲数据被丢弃并计数")
        print(f"\n[producer-cancelled] stats: {stats}")


class TestStatsOutput(unittest.TestCase):
    def test_stats_fields(self):
        pipe = Pipeline(capacity=4)
        pipe.add_stage(lambda x: x, name="id")
        pipe.start()
        for i in range(7):
            pipe.put(i)
        pipe.close()
        self.assertEqual(len(list(pipe)), 7)
        s = str(pipe.stats())
        for field in ("written=7", "processed=7", "dropped=0",
                      "failed=0", "peak_buffered="):
            self.assertIn(field, s)
        print(f"\n[stats-sample] {s}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
