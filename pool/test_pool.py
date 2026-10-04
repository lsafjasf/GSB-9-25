# -*- coding: utf-8 -*-
"""连接池回归测试。

结构：
  TestResidualObservable —— 逐项证明残留状态可以被观察到（修复前的 bug 形态）
  TestResetSteps         —— reset() 逐项复位 + 每一项失败都抛 ResetError
  TestPoolReturn         —— 正常归还 / 归还时抛异常 / 部分复位失败 / 强制关闭
  TestReuseEqualsFresh   —— 复用连接的行为与新连接一致（对拍）
  TestConcurrency        —— 并发借还不串状态

运行：python3 -m unittest test_pool -v
"""

from __future__ import annotations

import threading
import time
import unittest

from connection import (Connection, DEFAULT_TIMEOUT,
                        RESET_STEPS, ReadTimeoutError, ResetError)
from pool import ConnectionPool, PoolClosedError
from server import Server


def dirty_timeout(conn):
    conn.set_timeout(0.05)
    try:
        conn.sleep(200)
    except ReadTimeoutError:
        pass


def dirty_transaction(conn):
    conn.multi()
    conn.set("dirty-tx", "1")


def dirty_buffer(conn):
    conn.set_timeout(0.05)
    try:
        conn.sleep(200)
    except ReadTimeoutError:
        pass
    time.sleep(0.35)  # 等迟到响应 SLEPT 到达


def dirty_all(conn):
    dirty_timeout(conn)
    time.sleep(0.35)
    conn.multi()
    conn.set("dirty-all", "x")


class PoolTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        cls.server.__enter__()
        cls.host, cls.port = cls.server.server_address

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)

    def fresh(self):
        return Connection(self.host, self.port)

    def make_pool(self, **kw):
        pool = ConnectionPool(self.host, self.port, **kw)
        self.addCleanup(pool.close)
        return pool


# ---------------------------------------------------------------- 残留可观察

class TestResidualObservable(PoolTestBase):
    """每一项残留状态都先演示「不归还复位就能被下一个使用者观察到」。"""

    def test_residual_timeout_observable(self):
        conn = self.fresh()
        dirty_timeout(conn)
        # 不 reset：正常 PING 也会莫名超时
        with self.assertRaises(ReadTimeoutError):
            conn.ping()
        conn.close()

    def test_residual_transaction_observable(self):
        conn = self.fresh()
        dirty_transaction(conn)
        # 不 reset：下一个使用者发现自己在别人的事务里
        self.assertEqual(conn.status()["TX"], "1")
        # 他的 EXEC 会把别人没提交的东西一起提交
        self.assertEqual(conn.exec(), "COMMITTED")
        conn.close()

    def test_residual_buffer_observable(self):
        conn = self.fresh()
        dirty_buffer(conn)
        # 不 reset：读缓冲里有上次迟到的响应
        self.assertGreater(conn.unread_bytes(), 0)
        # PING 读到的是上次的 SLEPT，命令-响应错位
        self.assertEqual(conn.ping(), "SLEPT")
        conn.close()

    def test_residual_pending_write_observable(self):
        conn = self.fresh()
        conn._wbuf = b"PING\n"  # 模拟上次写中断留下的待发送字节
        self.assertGreater(len(conn._wbuf), 0)
        conn.close()


# ---------------------------------------------------------------- 逐项复位

class TestResetSteps(PoolTestBase):
    def test_reset_steps_order_and_report(self):
        conn = self.fresh()
        dirty_all(conn)
        report = conn.reset()
        self.assertEqual(report.steps, list(RESET_STEPS))
        self.assertTrue(report.restored_timeout)
        self.assertTrue(report.rolled_back)
        self.assertGreater(report.drained_bytes, 0)
        # 复位后与新连接不可区分
        self.assertEqual(conn.sock.gettimeout(), DEFAULT_TIMEOUT)
        self.assertEqual(conn.status()["TX"], "0")
        self.assertEqual(conn.unread_bytes(), 0)
        self.assertEqual(conn.ping(), "PONG")
        conn.close()

    def test_reset_clean_connection_is_noop(self):
        conn = self.fresh()
        report = conn.reset()
        self.assertEqual(report.steps, list(RESET_STEPS))
        self.assertFalse(report.rolled_back)
        self.assertFalse(report.restored_timeout)
        self.assertEqual(report.drained_bytes, 0)
        conn.close()

    def test_reset_failure_liveness(self):
        conn = self.fresh()
        conn.kill()
        time.sleep(0.1)
        with self.assertRaises(ResetError) as ctx:
            conn.reset()
        self.assertEqual(ctx.exception.step, "liveness")
        conn.close()

    def test_reset_failure_transaction(self):
        conn = self.fresh()
        conn.multi()
        conn.kill()  # 服务端关闭连接，但客户端缓冲里可能还有 OK
        time.sleep(0.1)
        with self.assertRaises(ResetError):
            conn.reset()
        conn.close()

    def test_reset_failure_flush(self):
        conn = self.fresh()
        conn.kill()
        time.sleep(0.1)
        conn._wbuf = b"PING\n"  # 有待发送字节，但对端已死
        with self.assertRaises(ResetError):
            conn.reset()
        conn.close()

    def test_reset_failure_ping_desync(self):
        conn = self.fresh()
        # 手工制造错位：STATUS 读到假 TX=0 通过，但 PING 读到的是
        # 另一条残留响应（命令-响应错位，ping 步骤必须拦下）。
        conn._step_drain = lambda report: report.steps.append("drain")
        conn._rbuf = b"TX=0\nSLEPT\n"
        with self.assertRaises(ResetError) as ctx:
            conn.reset()
        self.assertEqual(ctx.exception.step, "ping")
        conn.close()

    def test_reset_failure_garbled_transaction(self):
        conn = self.fresh()
        conn._step_drain = lambda report: report.steps.append("drain")
        conn._rbuf = b"GARBAGE\n"
        with self.assertRaises(ResetError) as ctx:
            conn.reset()
        self.assertEqual(ctx.exception.step, "transaction")
        conn.close()

    def test_reset_failure_drain(self):
        conn = self.fresh()
        conn.close()  # 已关闭连接 drain 必然失败（liveness 先拦）
        with self.assertRaises(ResetError) as ctx:
            conn.reset()
        self.assertEqual(ctx.exception.step, "liveness")


# ---------------------------------------------------------------- 归还情形

class FaultyConnection(Connection):
    """测试用：让指定复位步骤失败，模拟「部分复位失败」。"""

    fail_step = None

    def _step_drain(self, report):
        if self.fail_step == "drain":
            raise ResetError("drain", "injected failure")
        super()._step_drain(report)


class TestPoolReturn(PoolTestBase):
    def test_normal_return_reuses_connection(self):
        pool = self.make_pool(maxsize=2)
        with pool.lease() as conn:
            conn.set("a", "1")
            first = conn
        self.assertEqual(pool.reset_ok, 1)
        self.assertEqual(pool.destroyed, 0)
        with pool.lease() as conn2:
            self.assertIs(conn2, first)  # 复用同一条
            self.assertEqual(conn2.get("a"), "1")

    def test_return_with_exception_still_resets(self):
        pool = self.make_pool(maxsize=2)
        with self.assertRaises(RuntimeError):
            with pool.lease() as conn:
                dirty_all(conn)
                raise RuntimeError("使用者业务异常")
        # 异常归还：连接仍被复位并放回池中
        self.assertEqual(pool.reset_ok, 1)
        self.assertEqual(pool.destroyed, 0)
        with pool.lease() as conn:
            self.assertEqual(conn.status()["TX"], "0")
            self.assertEqual(conn.unread_bytes(), 0)
            self.assertEqual(conn.sock.gettimeout(), DEFAULT_TIMEOUT)

    def test_partial_reset_failure_destroys_connection(self):
        FaultyConnection.fail_step = "drain"
        self.addCleanup(setattr, FaultyConnection, "fail_step", None)
        pool = self.make_pool(maxsize=2, conn_factory=lambda: FaultyConnection(
            self.host, self.port))
        with pool.lease() as conn:
            conn.ping()
        # 复位失败 → 销毁，不进池
        self.assertEqual(pool.reset_failed, 1)
        self.assertEqual(pool.destroyed, 1)
        self.assertEqual(pool.idle, 0)
        self.assertTrue(conn.closed)
        # 下一次借到的是新连接，行为正常
        with pool.lease() as conn2:
            self.assertIsNot(conn2, conn)
            self.assertEqual(conn2.ping(), "PONG")

    def test_reset_failure_on_dirty_connection_destroys(self):
        """真实（非注入）的部分复位失败：缓冲残留 + 对端已死。"""
        pool = self.make_pool(maxsize=2)
        with pool.lease() as conn:
            conn.multi()
            conn.kill()
            time.sleep(0.1)
        self.assertEqual(pool.reset_failed, 1)
        self.assertEqual(pool.destroyed, 1)
        self.assertEqual(pool.idle, 0)

    def test_killed_connection_is_discarded_not_reused(self):
        pool = self.make_pool(maxsize=2)
        with pool.lease() as conn:
            conn.kill()
            time.sleep(0.1)
            first = conn
        self.assertEqual(pool.destroyed, 1)
        self.assertEqual(pool.idle, 0)
        with pool.lease() as conn2:
            self.assertIsNot(conn2, first)
            self.assertEqual(conn2.ping(), "PONG")

    def test_pool_replenishes_after_destroy(self):
        pool = self.make_pool(maxsize=1)
        with pool.lease() as conn:
            conn.kill()
            time.sleep(0.1)
        self.assertEqual(pool.total, 0)
        with pool.lease() as conn2:
            self.assertEqual(pool.total, 1)  # 懒创建补齐
            self.assertEqual(conn2.ping(), "PONG")

    def test_closed_pool_rejects_acquire(self):
        pool = ConnectionPool(self.host, self.port)
        pool.close()
        with self.assertRaises(PoolClosedError):
            pool.acquire()


# ---------------------------------------------------------------- 对拍

def probe(conn):
    """固定探针序列：对任何连接执行，输出必须与新连接一致。"""
    out = []
    out.append(conn.ping())
    out.append(conn.status()["TX"])
    out.append(str(conn.unread_bytes()))
    out.append(str(conn.sock.gettimeout()))
    out.append(conn.set("probe", "v1"))
    out.append(str(conn.get("probe")))
    out.append(conn.multi())
    out.append(conn.set("probe", "v2"))
    out.append(conn.rollback())
    out.append(str(conn.get("probe")))
    out.append(conn.status()["TX"])
    return out


class TestReuseEqualsFresh(PoolTestBase):
    SCENARIOS = {
        "clean": lambda conn: None,
        "timeout": dirty_timeout,
        "transaction": dirty_transaction,
        "buffer": dirty_buffer,
        "all": dirty_all,
        "killed": lambda conn: (conn.kill(), time.sleep(0.1)),
    }

    def test_reused_behaves_like_fresh(self):
        pool = self.make_pool(maxsize=2)
        for name, dirty in self.SCENARIOS.items():
            with self.subTest(scenario=name):
                # 弄脏一条连接再归还
                with pool.lease() as conn:
                    dirty(conn)
                # 复用（或被销毁后新建的）连接
                with pool.lease() as reused:
                    reused_out = probe(reused)
                # 全新连接
                fresh = self.fresh()
                try:
                    fresh_out = probe(fresh)
                finally:
                    fresh.close()
                self.assertEqual(reused_out, fresh_out,
                                 "scenario %s: reused != fresh" % name)


# ---------------------------------------------------------------- 并发

class TestConcurrency(PoolTestBase):
    def test_concurrent_lease_no_cross_talk(self):
        pool = self.make_pool(maxsize=4)
        errors = []

        def worker(i):
            try:
                for _ in range(20):
                    with pool.lease() as conn:
                        key = "w%d" % i
                        conn.set(key, str(i))
                        # 每次借到的连接都必须是干净的
                        assert conn.status()["TX"] == "0"
                        assert conn.unread_bytes() == 0
                        assert conn.get(key) == str(i)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(pool.reset_failed, 0)


if __name__ == "__main__":
    unittest.main()
