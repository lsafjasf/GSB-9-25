"""test_pool_reset.py — 回归测试（unittest，仅标准库）。

运行：python3 -m unittest test_pool_reset -v

覆盖：
  A. 可观察残留测试（旧 bug 行为下每类状态都能被下一个使用者观察到）
  B. 正常归还：复位后快照 == 新连接基准
  C. 归还时抛异常（业务异常 / 复位动作抛错）→ 连接销毁不放回
  D. 部分复位失败（静默 noop，校验捕获）→ 连接销毁不放回
  E. 连接被强制关闭（借出期间 / 池内空闲时）
  F. 复位清单完整性：实现与清单一致、清单覆盖全部可观察状态
"""

from __future__ import annotations

import unittest

from pool import ConnectionPool, ResetError
from simconn import (
    DEFAULT_STATEMENT_TIMEOUT_MS,
    ConnectionClosed,
    InFailedTransaction,
    SimConnection,
)

FRESH = SimConnection.fresh_snapshot()


def make_dirty(conn: SimConnection) -> None:
    """把全部 8 类状态都弄脏。"""
    conn.set_autocommit(False)
    conn.begin()
    conn.execute_write("INSERT INTO t VALUES (1)")
    conn.set_statement_timeout(5000)
    conn.execute_query("SELECT * FROM t", rows=[(1,), (2,)])
    conn.set_session_var("application_name", "dirty")
    conn.prepare("dirty_stmt")
    conn.execute_write("CREATE TEMP TABLE dirty_tmp (id int)")


class TestResidueObservable(unittest.TestCase):
    """A. 证明每类残留状态都能被下一个使用者观察到（旧 bug 行为）。"""

    def observe(self, dirty_fn, key):
        pool = ConnectionPool(legacy_bug_mode=True)
        conn = pool.acquire()
        dirty_fn(conn)
        pool.release(conn)
        nxt = pool.acquire()
        self.assertIs(nxt, conn, "旧池应复用同一物理连接")
        return nxt.snapshot()[key]

    def test_uncommitted_transaction_observable(self):
        def dirty(c):
            c.set_autocommit(False)
            c.begin()
            c.execute_write("INSERT INTO t VALUES (1)")
        self.assertTrue(self.observe(dirty, "in_transaction"))
        self.assertNotEqual(FRESH["in_transaction"], True)

    def test_aborted_transaction_observable(self):
        def dirty(c):
            c.set_autocommit(False)
            c.begin()
            try:
                c.execute_write("SELECT BROKEN")
            except InFailedTransaction:
                pass
        self.assertTrue(self.observe(dirty, "txn_aborted"))

    def test_statement_timeout_observable(self):
        val = self.observe(lambda c: c.set_statement_timeout(5000), "statement_timeout_ms")
        self.assertEqual(val, 5000)
        self.assertEqual(FRESH["statement_timeout_ms"], DEFAULT_STATEMENT_TIMEOUT_MS)

    def test_unread_results_observable(self):
        def dirty(c):
            c.execute_query("SELECT * FROM t", rows=[(1,), (2,), (3,)])
        self.assertEqual(self.observe(dirty, "unread_rows"), 3)

    def test_autocommit_flag_observable(self):
        self.assertFalse(self.observe(lambda c: c.set_autocommit(False), "autocommit"))

    def test_session_vars_observable(self):
        val = self.observe(
            lambda c: c.set_session_var("application_name", "tenant-A"), "session_vars"
        )
        self.assertEqual(val, {"application_name": "tenant-A"})

    def test_prepared_statements_observable(self):
        val = self.observe(lambda c: c.prepare("stmt_a"), "prepared_statements")
        self.assertEqual(val, {"stmt_a"})

    def test_temp_tables_observable(self):
        val = self.observe(
            lambda c: c.execute_write("CREATE TEMP TABLE tmp_a (id int)"), "temp_tables"
        )
        self.assertEqual(val, {"tmp_a"})


class TestNormalRelease(unittest.TestCase):
    """B. 正常归还：全部状态复位，复用连接与新连接一致。"""

    def test_full_reset_on_release(self):
        pool = ConnectionPool()
        conn = pool.acquire()
        make_dirty(conn)
        pool.release(conn)
        self.assertEqual(conn.snapshot(), FRESH)
        self.assertEqual(pool.idle_count, 1)

    def test_reused_connection_matches_fresh(self):
        pool = ConnectionPool()
        conn = pool.acquire()
        make_dirty(conn)
        pool.release(conn)
        reused = pool.acquire()
        self.assertIs(reused, conn)
        self.assertEqual(reused.snapshot(), FRESH)
        # 行为对拍：复用连接上跑完整事务周期不报错
        reused.set_autocommit(False)
        reused.begin()
        reused.execute_write("INSERT INTO t VALUES (1)")
        reused.rollback()
        rows = reused.execute_query("SELECT 1", rows=[("ok",)])
        self.assertEqual(reused.fetchall(), rows)

    def test_clean_release_reuses_connection(self):
        pool = ConnectionPool()
        conn = pool.acquire()
        pool.release(conn)
        self.assertIs(pool.acquire(), conn)
        self.assertEqual(pool.stats["reused"], 1)
        self.assertEqual(pool.stats["destroyed"], 0)


class TestExceptionOnRelease(unittest.TestCase):
    """C. 归还路径上的异常：业务异常经 with 归还、复位动作抛错。"""

    def test_business_exception_still_resets_and_reuses(self):
        pool = ConnectionPool()
        with self.assertRaises(ValueError):
            with pool.borrow() as conn:
                make_dirty(conn)
                raise ValueError("business failure")
        self.assertEqual(conn.snapshot(), FRESH)
        self.assertEqual(pool.idle_count, 1)

    def test_reset_action_raise_destroys_connection(self):
        pool = ConnectionPool()
        conn = pool.acquire()
        make_dirty(conn)
        conn.fail_at["rollback"] = "raise"  # 复位到回滚这一步抛错
        pool.release(conn)
        self.assertTrue(conn.closed)
        self.assertEqual(pool.idle_count, 0)
        self.assertEqual(pool.stats["reset_failures"], 1)
        self.assertEqual(pool.stats["destroyed"], 1)
        # 下一次借到的是新连接
        nxt = pool.acquire()
        self.assertIsNot(nxt, conn)
        self.assertEqual(nxt.snapshot(), FRESH)

    def test_reset_error_reports_step(self):
        pool = ConnectionPool()
        conn = pool.acquire()
        conn.set_statement_timeout(5000)
        conn.fail_at["reset_timeout"] = "raise"
        with self.assertRaises(ResetError) as ctx:
            pool._reset(conn)
        self.assertEqual(ctx.exception.step, "timeout")


class TestPartialResetFailure(unittest.TestCase):
    """D. 部分复位失败：动作静默无效（noop），校验必须捕获并销毁。"""

    # (fail_at 动作, 弄脏方式, 期望失败步骤)
    CASES = [
        ("drain_results", lambda c: c.execute_query("Q", rows=[(1,)]), "drain_results"),
        ("rollback", lambda c: (c.set_autocommit(False), c.begin(),
                                c.execute_write("INSERT INTO t VALUES (1)")), "rollback_txn"),
        ("reset_timeout", lambda c: c.set_statement_timeout(5000), "timeout"),
        ("reset_autocommit", lambda c: c.set_autocommit(False), "autocommit"),
        ("reset_session_vars", lambda c: c.set_session_var("k", "v"), "session_vars"),
        ("deallocate_all", lambda c: c.prepare("s"), "prepared_stmts"),
        ("drop_temp_tables", lambda c: c.execute_write("CREATE TEMP TABLE t1 (id int)"),
         "temp_tables"),
        ("ping", lambda c: None, "health_check"),
    ]

    def test_every_step_noop_is_caught_and_destroys(self):
        for action, dirty, step in self.CASES:
            with self.subTest(step=step):
                pool = ConnectionPool()
                conn = pool.acquire()
                dirty(conn)
                conn.fail_at[action] = "noop"
                pool.release(conn)
                self.assertTrue(conn.closed, f"{step}: 连接应被销毁")
                self.assertEqual(pool.idle_count, 0, f"{step}: 不得放回池")
                self.assertEqual(pool.stats["reset_failures"], 1)
                nxt = pool.acquire()
                self.assertIsNot(nxt, conn)
                self.assertEqual(nxt.snapshot(), FRESH)

    def test_reset_error_identifies_failed_step(self):
        for action, dirty, step in self.CASES:
            with self.subTest(step=step):
                conn = SimConnection()
                dirty(conn)
                conn.fail_at[action] = "noop"
                pool = ConnectionPool()
                with self.assertRaises(ResetError) as ctx:
                    pool._reset(conn)
                self.assertEqual(ctx.exception.step, step)


class TestForceClose(unittest.TestCase):
    """E. 连接被强制关闭：借出期间与池内空闲两种情形。"""

    def test_force_close_while_borrowed(self):
        pool = ConnectionPool()
        conn = pool.acquire()
        conn.force_close()  # 服务端 kill / 网络中断
        pool.release(conn)
        self.assertEqual(pool.idle_count, 0, "死连接不得放回池")
        self.assertEqual(pool.stats["destroyed"], 1)
        nxt = pool.acquire()
        self.assertIsNot(nxt, conn)
        self.assertEqual(nxt.snapshot(), FRESH)

    def test_force_close_while_idle_in_pool(self):
        pool = ConnectionPool()
        conn = pool.acquire()
        pool.release(conn)
        self.assertEqual(pool.idle_count, 1)
        conn.force_close()  # 空闲期间被服务端断开
        nxt = pool.acquire()  # 借出时健康检查应剔除
        self.assertIsNot(nxt, conn)
        self.assertEqual(nxt.snapshot(), FRESH)
        self.assertEqual(pool.stats["destroyed"], 1)

    def test_operations_on_closed_connection_raise(self):
        conn = SimConnection()
        conn.force_close()
        with self.assertRaises(ConnectionClosed):
            conn.execute_query("SELECT 1")
        with self.assertRaises(ConnectionClosed):
            conn.ping()


class TestResetChecklistIntegrity(unittest.TestCase):
    """F. 复位清单完整性：清单即实现，且覆盖全部可观察状态。"""

    def test_every_snapshot_key_is_reset_or_inherent(self):
        # 这些键由连接生命周期本身保证（open/healthy 由 health_check 与销毁策略覆盖）
        inherent = {"open", "healthy"}
        resettable = set(FRESH) - inherent
        covered = {key for _, _, key, _ in ConnectionPool.RESET_STEPS}
        covered |= {"open"}  # health_check 步校验 open
        missing = resettable - covered
        self.assertEqual(missing, set(), f"清单未覆盖状态: {missing}")

    def test_checklist_order_is_safe(self):
        # 必须先排空结果再发命令，必须先回滚再改会话状态，健康检查必须最后
        steps = [s for s, *_ in ConnectionPool.RESET_STEPS]
        self.assertLess(steps.index("drain_results"), steps.index("rollback_txn"))
        self.assertLess(steps.index("rollback_txn"), steps.index("session_vars"))
        self.assertEqual(steps[-1], "health_check")

    def test_no_extra_resettable_state_exists(self):
        # 防止将来给 SimConnection 加了新状态却忘了加复位步骤
        conn = SimConnection()
        make_dirty(conn)
        pool = ConnectionPool()
        pool._reset(conn)
        self.assertEqual(conn.snapshot(), FRESH)


if __name__ == "__main__":
    unittest.main()
