"""object_pool.ObjectPool 回归测试。

运行：python3 -m unittest test_object_pool -v
覆盖：异常路径归还、并发获取/归还的独占不变量、计数不变量、
      获取超时语义、池关闭语义（在途对象 / 等待者 / 关闭后获取）。
"""

import threading
import time
import unittest

from object_pool import (
    ObjectPool,
    PoolClosed,
    PoolTimeout,
    ReleaseError,
)


class FakeConnection:
    _seq = 0
    _seq_lock = threading.Lock()

    def __init__(self):
        with FakeConnection._seq_lock:
            FakeConnection._seq += 1
            self.ident = FakeConnection._seq
        self.closed = False

    def close(self):
        self.closed = True


def make_pool(max_size=2):
    return ObjectPool(FakeConnection, max_size=max_size)


class TestExceptionPath(unittest.TestCase):
    """缺陷1 回归：异常路径也必须归还对象。"""

    def test_context_manager_releases_on_exception(self):
        pool = make_pool(max_size=1)
        with self.assertRaises(RuntimeError):
            with pool.item() as conn:
                raise RuntimeError("业务异常")
        # 异常后对象已归还，可立即再次获取到同一个对象
        with pool.item(timeout=0.1) as again:
            self.assertIs(again, conn)
        pool.check_invariants()

    def test_try_finally_releases_on_exception(self):
        pool = make_pool(max_size=1)
        conn = pool.acquire()
        try:
            raise RuntimeError("业务异常")
        except RuntimeError:
            pass
        finally:
            pool.release(conn)
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (1, 0, 1))
        pool.check_invariants()

    def test_factory_failure_keeps_counts_consistent(self):
        calls = []

        def flaky_factory():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("connect failed")
            return FakeConnection()

        pool = ObjectPool(flaky_factory, max_size=1)
        with self.assertRaises(OSError):
            pool.acquire()
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (0, 0, 0))
        conn = pool.acquire(timeout=0.1)  # 工厂失败后仍可正常获取
        self.assertIsInstance(conn, FakeConnection)
        pool.check_invariants()


class TestDoubleRelease(unittest.TestCase):
    """缺陷2 回归：重复归还/非法归还被拒绝。"""

    def test_double_release_raises(self):
        pool = make_pool(max_size=1)
        conn = pool.acquire()
        pool.release(conn)
        with self.assertRaises(ReleaseError):
            pool.release(conn)
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (1, 0, 1))
        pool.check_invariants()

    def test_foreign_object_raises(self):
        pool = make_pool(max_size=1)
        with self.assertRaises(ReleaseError):
            pool.release(FakeConnection())
        pool.check_invariants()


class TestMaxSize(unittest.TestCase):
    """缺陷3 回归：任何情况下都不超过上限。"""

    def test_never_exceeds_max_size(self):
        pool = make_pool(max_size=1)
        pool.acquire()
        with self.assertRaises(PoolTimeout):
            pool.acquire(timeout=0.1)
        stats = pool.stats()
        self.assertEqual(stats.total, 1)
        self.assertEqual(stats.peak, 1)
        pool.check_invariants()


class TestTimeout(unittest.TestCase):
    """超时语义：抛 PoolTimeout，且绝不返回已销毁对象。"""

    def test_timeout_raises_and_waits(self):
        pool = make_pool(max_size=1)
        pool.acquire()
        before = time.monotonic()
        with self.assertRaises(PoolTimeout):
            pool.acquire(timeout=0.2)
        self.assertGreaterEqual(time.monotonic() - before, 0.19)
        pool.check_invariants()

    def test_zero_timeout_raises_immediately(self):
        pool = make_pool(max_size=1)
        pool.acquire()
        before = time.monotonic()
        with self.assertRaises(PoolTimeout):
            pool.acquire(timeout=0)
        self.assertLess(time.monotonic() - before, 0.1)

    def test_acquired_object_is_never_destroyed(self):
        # 缺陷4 回归：超时路径附近返回的对象必须未被销毁
        pool = make_pool(max_size=1)
        first = pool.acquire()
        pool.release(first)
        got = pool.acquire(timeout=0.5)
        self.assertFalse(got.closed)
        pool.check_invariants()


class TestClose(unittest.TestCase):
    """关闭语义：空闲对象销毁、等待者唤醒、在途对象归还时销毁、关闭后获取报错。"""

    def test_close_destroys_idle_objects(self):
        pool = make_pool(max_size=2)
        a, b = pool.acquire(), pool.acquire()
        pool.release(a)
        pool.release(b)
        pool.close()
        self.assertTrue(a.closed and b.closed)
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (0, 0, 0))
        self.assertTrue(stats.closed)
        pool.check_invariants()

    def test_close_wakes_waiters_with_pool_closed(self):
        pool = make_pool(max_size=1)
        pool.acquire()  # 占满，令等待者阻塞
        errors = []

        def waiter():
            try:
                pool.acquire()  # 无限等待，直到池关闭
            except PoolClosed:
                errors.append("PoolClosed")

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        pool.close()
        t.join(timeout=2)
        self.assertEqual(errors, ["PoolClosed"])
        self.assertFalse(t.is_alive())

    def test_inflight_object_destroyed_on_release_after_close(self):
        pool = make_pool(max_size=1)
        conn = pool.acquire()
        pool.close()
        self.assertFalse(conn.closed)  # 在途对象不被提前销毁
        pool.release(conn)             # 归还时销毁，不再入池
        self.assertTrue(conn.closed)
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (0, 0, 0))
        pool.check_invariants()

    def test_acquire_after_close_raises(self):
        pool = make_pool(max_size=1)
        pool.close()
        with self.assertRaises(PoolClosed):
            pool.acquire(timeout=0.05)

    def test_close_is_idempotent(self):
        pool = make_pool(max_size=1)
        pool.close()
        pool.close()
        pool.check_invariants()

    def test_no_destroyed_object_after_close_race(self):
        # 缺陷4 回归：归还与关闭并发时，获取方要么拿到活对象，要么抛异常
        for _ in range(50):
            pool = make_pool(max_size=1)
            conn = pool.acquire()
            outcome = {}

            def waiter():
                try:
                    outcome["conn"] = pool.acquire(timeout=1)
                except (PoolClosed, PoolTimeout) as exc:
                    outcome["error"] = exc

            t = threading.Thread(target=waiter)
            t.start()
            pool.release(conn)
            pool.close()
            t.join()
            if "conn" in outcome:
                self.assertFalse(outcome["conn"].closed)
            else:
                self.assertIsInstance(outcome["error"], PoolClosed)
            pool.check_invariants()


class TestConcurrency(unittest.TestCase):
    """并发获取/归还：断言独占不变量与计数不变量。"""

    THREADS = 8
    ROUNDS = 300

    def test_exclusivity_and_counter_invariants(self):
        max_size = 3
        pool = make_pool(max_size=max_size)
        held = {}               # obj -> 持有者线程，用于独占断言
        held_lock = threading.Lock()
        concurrent = 0          # 当前持有对象的总人数
        max_concurrent = 0
        violations = []
        stop_monitor = threading.Event()

        def worker():
            nonlocal concurrent, max_concurrent
            for _ in range(self.ROUNDS):
                try:
                    conn = pool.acquire(timeout=5)
                except PoolTimeout:
                    violations.append("unexpected PoolTimeout")
                    return
                with held_lock:
                    if conn in held:
                        violations.append(
                            f"独占性被破坏: {conn!r} 同时被两个调用方持有"
                        )
                    held[conn] = threading.get_ident()
                    concurrent += 1
                    max_concurrent = max(max_concurrent, concurrent)
                    if concurrent > max_size:
                        violations.append(
                            f"并发持有者 {concurrent} 超过上限 {max_size}"
                        )
                time.sleep(0)  # 让出 GIL，制造交错
                with held_lock:
                    held.pop(conn, None)
                    concurrent -= 1
                pool.release(conn)

        def monitor():
            # 在并发进行期间持续断言计数不变量
            while not stop_monitor.is_set():
                try:
                    pool.check_invariants()
                except AssertionError as exc:
                    violations.append(f"计数不变量被破坏: {exc}")
                    return

        threads = [threading.Thread(target=worker) for _ in range(self.THREADS)]
        mon = threading.Thread(target=monitor)
        mon.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop_monitor.set()
        mon.join()

        self.assertEqual(violations, [])
        self.assertGreater(max_concurrent, 1)  # 确实发生了并发
        self.assertLessEqual(max_concurrent, max_size)
        stats = pool.stats()
        self.assertEqual(stats.in_use, 0)
        self.assertEqual(stats.idle, stats.total)
        self.assertEqual(stats.peak, max_size)
        pool.check_invariants()

    def test_concurrent_close(self):
        # 多线程借还过程中关闭池：不死锁、不返回已销毁对象、计数最终归零
        pool = make_pool(max_size=3)
        errors = []

        def worker():
            while True:
                try:
                    with pool.item(timeout=5) as conn:
                        self.assertFalse(conn.closed)
                        time.sleep(0)
                except PoolClosed:
                    return
                except Exception as exc:  # noqa: BLE001 - 收集所有意外异常
                    errors.append(exc)
                    return

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        time.sleep(0.05)
        pool.close()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertFalse(any(t.is_alive() for t in threads))
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (0, 0, 0))
        pool.check_invariants()


class TestCounters(unittest.TestCase):
    """缺陷5 回归：计数在任意时刻与实际一致。"""

    def test_counts_track_reality(self):
        pool = make_pool(max_size=3)
        self.assertEqual(pool.stats()._asdict(),
                         dict(idle=0, in_use=0, total=0, peak=0,
                              max_size=3, closed=False))
        a = pool.acquire()
        b = pool.acquire()
        self.assertEqual(pool.stats()[:4], (0, 2, 2, 2))
        pool.release(a)
        self.assertEqual(pool.stats()[:4], (1, 1, 2, 2))
        pool.release(b)
        self.assertEqual(pool.stats()[:4], (2, 0, 2, 2))
        c = pool.acquire()  # 复用空闲对象，不新建
        self.assertIn(c, (a, b))
        self.assertEqual(pool.stats()[:4], (1, 1, 2, 2))
        pool.close()
        pool.release(c)  # 关闭后归还：销毁并扣减
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (0, 0, 0))
        self.assertEqual(stats.peak, 2)  # 峰值保留历史
        pool.check_invariants()


if __name__ == "__main__":
    unittest.main()
