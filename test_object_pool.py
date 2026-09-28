"""object_pool.ObjectPool 回归测试。

运行：python3 -m unittest test_object_pool -v
覆盖：异常路径归还、并发获取/归还的独占不变量、计数不变量、
      获取超时语义、池关闭语义（在途对象 / 等待者 / 关闭后获取）、
      陈旧二次归还（借出凭据失效校验）。
"""

import collections
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
        lease = pool.acquire()
        try:
            raise RuntimeError("业务异常")
        except RuntimeError:
            pass
        finally:
            pool.release(lease)
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
        lease = pool.acquire(timeout=0.1)  # 工厂失败后仍可正常获取
        self.assertIsInstance(lease.obj, FakeConnection)
        pool.check_invariants()


class TestDoubleRelease(unittest.TestCase):
    """缺陷2 回归：重复归还/非法归还/陈旧归还被拒绝。"""

    def test_double_release_raises(self):
        pool = make_pool(max_size=1)
        lease = pool.acquire()
        pool.release(lease)
        with self.assertRaises(ReleaseError):
            pool.release(lease)
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (1, 0, 1))
        pool.check_invariants()

    def test_foreign_object_raises(self):
        pool = make_pool(max_size=1)
        with self.assertRaises(ReleaseError):
            pool.release(FakeConnection())
        pool.check_invariants()

    def test_raw_object_is_not_a_credential(self):
        # 归还只认凭据：直接拿对象本身归还同样被拒绝
        pool = make_pool(max_size=1)
        lease = pool.acquire()
        with self.assertRaises(ReleaseError):
            pool.release(lease.obj)
        pool.release(lease)  # 凭据本身仍可正常归还
        pool.check_invariants()


class TestStaleRelease(unittest.TestCase):
    """陈旧二次归还：旧凭据失效后，无法把他人持有的对象“还”回池里。"""

    def test_stale_lease_rejected_after_recheckout(self):
        # 确定性时序：甲归还 -> 乙借到同一对象 -> 甲用旧凭据再次归还
        pool = make_pool(max_size=1)
        lease_a = pool.acquire()
        obj = lease_a.obj
        pool.release(lease_a)

        lease_b = pool.acquire()
        self.assertIs(lease_b.obj, obj)      # 乙复用了同一对象
        self.assertIsNot(lease_b, lease_a)   # 但签发的是新凭据

        with self.assertRaises(ReleaseError):  # 甲的陈旧归还被拒绝
            pool.release(lease_a)

        # 对象仍只在乙手中：第三方此时获取必须超时，而不是拿到同一对象
        with self.assertRaises(PoolTimeout):
            pool.acquire(timeout=0.05)
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (0, 1, 1))
        pool.check_invariants()

    def test_concurrent_stale_release_keeps_exclusivity(self):
        # 并发版：甲归还、乙持有、丙等待三方交错，
        # 断言同一对象在任何时刻只被一个调用方持有
        pool = make_pool(max_size=1)
        lease_a = pool.acquire()
        obj = lease_a.obj

        a_released = threading.Event()
        b_holding = threading.Event()
        c_attempting = threading.Event()
        c_done = threading.Event()
        finish = threading.Event()
        outcomes = {}
        violations = []

        def holder_b():
            a_released.wait(2)
            lease_b = pool.acquire(timeout=2)
            outcomes["lease_b"] = lease_b
            b_holding.set()
            finish.wait(2)
            pool.release(lease_b)

        def waiter_c():
            # 第三方：在乙持有期间尝试获取，必须超时而非拿到同一对象
            b_holding.wait(2)
            c_attempting.set()
            try:
                lease_c = pool.acquire(timeout=0.2)
            except PoolTimeout:
                outcomes["c"] = "timeout"
            else:
                violations.append("乙持有期间第三方拿到了同一对象")
                pool.release(lease_c)
            finally:
                c_done.set()

        tb = threading.Thread(target=holder_b)
        tc = threading.Thread(target=waiter_c)
        tb.start()
        tc.start()

        pool.release(lease_a)  # 甲正常归还
        a_released.set()
        self.assertTrue(b_holding.wait(2))
        self.assertTrue(c_attempting.wait(2))
        time.sleep(0.05)  # 确认丙已阻塞在获取上

        # 甲的陈旧二次归还（与乙的持有、丙的等待并发）：必须被拒绝
        with self.assertRaises(ReleaseError):
            pool.release(lease_a)

        # 丙必须在乙仍持有期间超时，而不是拿到同一对象
        self.assertTrue(c_done.wait(2))
        finish.set()
        tb.join(2)
        tc.join(2)

        self.assertIs(outcomes["lease_b"].obj, obj)
        self.assertEqual(outcomes.get("c"), "timeout")
        self.assertEqual(violations, [])
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (1, 0, 1))
        pool.check_invariants()

    def test_stale_release_storm(self):
        # 压力版：多线程正常借还的同时，破坏者持续用已失效的旧凭据尝试归还
        pool = make_pool(max_size=2)
        retired = collections.deque(maxlen=64)  # 已归还、已失效的凭据
        retired_lock = threading.Lock()
        violations = []
        stop = threading.Event()

        def worker():
            for _ in range(200):
                lease = pool.acquire(timeout=5)
                time.sleep(0)
                pool.release(lease)
                with retired_lock:
                    retired.append(lease)

        def saboteur():
            while not stop.is_set():
                with retired_lock:
                    stale = list(retired)
                for lease in stale:
                    try:
                        pool.release(lease)
                    except ReleaseError:
                        pass
                    else:
                        violations.append("陈旧凭据归还成功，独占性被破坏")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        sab = threading.Thread(target=saboteur)
        sab.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        sab.join()

        self.assertEqual(violations, [])
        stats = pool.stats()
        self.assertEqual(stats.in_use, 0)
        self.assertEqual(stats.idle, stats.total)
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
        lease = pool.acquire()
        pool.release(lease)
        got = pool.acquire(timeout=0.5)
        self.assertFalse(got.obj.closed)
        pool.check_invariants()


class TestClose(unittest.TestCase):
    """关闭语义：空闲对象销毁、等待者唤醒、在途对象归还时销毁、关闭后获取报错。"""

    def test_close_destroys_idle_objects(self):
        pool = make_pool(max_size=2)
        a, b = pool.acquire(), pool.acquire()
        pool.release(a)
        pool.release(b)
        pool.close()
        self.assertTrue(a.obj.closed and b.obj.closed)
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
        lease = pool.acquire()
        pool.close()
        self.assertFalse(lease.obj.closed)  # 在途对象不被提前销毁
        pool.release(lease)                 # 归还时销毁，不再入池
        self.assertTrue(lease.obj.closed)
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
            lease = pool.acquire()
            outcome = {}

            def waiter():
                try:
                    outcome["lease"] = pool.acquire(timeout=1)
                except (PoolClosed, PoolTimeout) as exc:
                    outcome["error"] = exc

            t = threading.Thread(target=waiter)
            t.start()
            pool.release(lease)
            pool.close()
            t.join()
            if "lease" in outcome:
                self.assertFalse(outcome["lease"].obj.closed)
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
                    lease = pool.acquire(timeout=5)
                except PoolTimeout:
                    violations.append("unexpected PoolTimeout")
                    return
                conn = lease.obj
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
                pool.release(lease)

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
        self.assertIn(c.obj, (a.obj, b.obj))
        self.assertEqual(pool.stats()[:4], (1, 1, 2, 2))
        pool.close()
        pool.release(c)  # 关闭后归还：销毁并扣减
        stats = pool.stats()
        self.assertEqual((stats.idle, stats.in_use, stats.total), (0, 0, 0))
        self.assertEqual(stats.peak, 2)  # 峰值保留历史
        pool.check_invariants()


if __name__ == "__main__":
    unittest.main()
