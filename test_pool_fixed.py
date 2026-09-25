"""回归测试：修复后的 ObjectPool。

覆盖：五类缺陷对应场景、并发获取/归还的独占不变量、
超时与池关闭语义、计数一致性不变量。
"""

import threading
import time
import unittest

from object_pool import AcquireTimeout, ObjectPool, PoolClosed


class FakeConnection:
    _seq = 0
    _seq_lock = threading.Lock()

    def __init__(self):
        with FakeConnection._seq_lock:
            FakeConnection._seq += 1
            self.id = FakeConnection._seq
        self.closed = False

    def close(self):
        self.closed = True


def make_pool(max_size=2):
    return ObjectPool(FakeConnection, max_size)


class TestExceptionSafety(unittest.TestCase):
    def test_context_manager_releases_on_exception(self):
        """修复缺陷1：with 块内抛异常，对象仍归还，池不枯竭。"""
        pool = make_pool(max_size=1)
        with self.assertRaises(RuntimeError):
            with pool.item():
                raise RuntimeError("business error")
        pool.check_invariants()
        self.assertEqual(pool.idle_count, 1)
        self.assertEqual(pool.in_use_count, 0)
        # 池未枯竭，还能再拿到
        with pool.item() as obj:
            self.assertFalse(obj.closed)

    def test_try_finally_releases_on_exception(self):
        """修复缺陷1（手写形态）：try/finally 保证归还。"""
        pool = make_pool(max_size=1)
        obj = pool.acquire()
        try:
            raise RuntimeError("business error")
        except RuntimeError:
            pass
        finally:
            pool.release(obj)
        pool.check_invariants()
        self.assertEqual(pool.idle_count, 1)


class TestDoubleRelease(unittest.TestCase):
    def test_double_release_rejected(self):
        """修复缺陷2：重复归还被拒绝，同一对象不会被两个调用方拿到。"""
        pool = make_pool(max_size=1)
        obj = pool.acquire()
        pool.release(obj)
        with self.assertRaises(ValueError):
            pool.release(obj)
        pool.check_invariants()
        a = pool.acquire(timeout=0.1)
        # 池已空，第二个获取只能超时，拿不到同一个对象
        with self.assertRaises(AcquireTimeout):
            pool.acquire(timeout=0.05)
        self.assertIs(a, obj)

    def test_foreign_object_rejected(self):
        pool = make_pool(max_size=1)
        with self.assertRaises(ValueError):
            pool.release(FakeConnection())


class TestMaxSize(unittest.TestCase):
    def test_never_exceeds_max_size(self):
        """修复缺陷3：达到上限后不再创建，只能等待或超时。"""
        pool = make_pool(max_size=2)
        a = pool.acquire()
        b = pool.acquire()
        with self.assertRaises(AcquireTimeout):
            pool.acquire(timeout=0.1)
        self.assertEqual(pool.total_count, 2)
        self.assertEqual(pool.peak_count, 2)
        pool.release(a)
        c = pool.acquire(timeout=0.1)   # 复用而非新建
        self.assertIs(c, a)
        self.assertEqual(pool.total_count, 2)
        pool.release(b)
        pool.release(c)
        pool.check_invariants()


class TestTimeoutAndClose(unittest.TestCase):
    def test_timeout_raises_and_never_returns_destroyed(self):
        """修复缺陷4：超时抛 AcquireTimeout，绝不返回已销毁对象。"""
        pool = make_pool(max_size=1)
        obj = pool.acquire()
        pool.release(obj)
        pool.close()
        self.assertTrue(obj.closed)
        with self.assertRaises(PoolClosed):
            pool.acquire(timeout=0.05)

    def test_timeout_while_full(self):
        pool = make_pool(max_size=1)
        pool.acquire()
        start = time.monotonic()
        with self.assertRaises(AcquireTimeout):
            pool.acquire(timeout=0.1)
        self.assertGreaterEqual(time.monotonic() - start, 0.09)

    def test_close_wakes_waiters(self):
        """关闭时等待者被唤醒并收到 PoolClosed。"""
        pool = make_pool(max_size=1)
        pool.acquire()  # 占满
        errors = []

        def waiter():
            try:
                pool.acquire()
            except PoolClosed:
                errors.append("closed")

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)  # 确保已进入等待
        pool.close()
        t.join(timeout=2)
        self.assertEqual(errors, ["closed"])

    def test_inflight_object_destroyed_on_release_after_close(self):
        """关闭时在途对象：归还即销毁，不回池。"""
        pool = make_pool(max_size=1)
        obj = pool.acquire()
        pool.close()
        pool.release(obj)
        self.assertTrue(obj.closed)
        self.assertEqual(pool.total_count, 0)
        pool.check_invariants()

    def test_acquire_after_close_raises(self):
        """关闭后获取：抛出 PoolClosed。"""
        pool = make_pool(max_size=1)
        pool.close()
        with self.assertRaises(PoolClosed):
            pool.acquire()
        with self.assertRaises(PoolClosed):
            with pool.item():
                pass

    def test_close_is_idempotent(self):
        pool = make_pool(max_size=1)
        pool.close()
        pool.close()


class TestCounters(unittest.TestCase):
    def test_counters_consistent_through_lifecycle(self):
        """修复缺陷5：计数在任意时刻与实际一致。"""
        pool = make_pool(max_size=3)
        self.assertEqual((pool.idle_count, pool.in_use_count,
                          pool.total_count, pool.peak_count), (0, 0, 0, 0))
        a = pool.acquire()
        b = pool.acquire()
        self.assertEqual((pool.idle_count, pool.in_use_count,
                          pool.total_count), (0, 2, 2))
        pool.release(a)
        self.assertEqual((pool.idle_count, pool.in_use_count,
                          pool.total_count), (1, 1, 2))
        pool.release(b)
        self.assertEqual((pool.idle_count, pool.in_use_count,
                          pool.total_count), (2, 0, 2))
        self.assertEqual(pool.peak_count, 2)
        pool.close()
        self.assertEqual((pool.idle_count, pool.in_use_count,
                          pool.total_count), (0, 0, 0))
        self.assertEqual(pool.peak_count, 2)  # 峰值保留历史
        pool.check_invariants()


class TestConcurrency(unittest.TestCase):
    def test_concurrent_exclusive_ownership_and_counters(self):
        """多线程反复获取/归还：任一时刻同一对象只被一个调用方持有，
        且计数不变量全程成立。"""
        pool = make_pool(max_size=3)
        monitor_lock = threading.Lock()
        holders = {}          # obj -> 持有线程数（不变量：<= 1）
        violations = []
        iterations = 400

        def worker():
            for _ in range(iterations):
                try:
                    with pool.item(timeout=5) as obj:
                        with monitor_lock:
                            holders[obj] = holders.get(obj, 0) + 1
                            if holders[obj] > 1:
                                violations.append(("shared", obj.id))
                        time.sleep(0)  # 让出 GIL，制造交错
                        with monitor_lock:
                            holders[obj] -= 1
                        pool.check_invariants()
                except (AcquireTimeout, PoolClosed) as exc:
                    violations.append(("unexpected", repr(exc)))

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
            self.assertFalse(t.is_alive(), "worker 线程未结束，疑似死锁")

        self.assertEqual(violations, [])
        self.assertEqual(pool.in_use_count, 0)
        self.assertLessEqual(pool.total_count, 3)
        self.assertEqual(pool.peak_count, 3)
        self.assertEqual(pool.idle_count, pool.total_count)
        pool.check_invariants()
        pool.close()

    def test_concurrent_acquire_release_with_close(self):
        """并发获取/归还期间关闭池：不死锁、不返回已销毁对象、计数归零。"""
        pool = make_pool(max_size=4)
        errors = []

        def worker():
            for _ in range(200):
                try:
                    with pool.item(timeout=2) as obj:
                        assert not obj.closed, "拿到已销毁对象"
                except (PoolClosed, AcquireTimeout):
                    pass
                except AssertionError as exc:
                    errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(0.05)
        pool.close()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "关闭后 worker 未退出")
        self.assertEqual(errors, [])
        self.assertEqual(pool.total_count, 0)
        pool.check_invariants()


if __name__ == "__main__":
    unittest.main(verbosity=2)
