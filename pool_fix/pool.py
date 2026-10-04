"""pool.py — 修复后的连接池。

核心修复：归还连接时执行**完整复位清单**（RESET_STEPS），
每一步都有动作 + 校验，任何一步失败（抛错或校验不通过）
都销毁该连接、绝不放回池中，下次借出时新建。

判断依据（为什么复位失败必须销毁而不是放回）：
  1. 复位动作失败说明连接可能处于半坏状态（协议流已错位），
     无法确定哪些状态已复位、哪些没有；
  2. 把状态未知的连接放回池，会把上一个使用者的脏状态
     （事务、超时、缓冲、会话变量……）泄漏给下一个使用者，
     这正是本任务要修的 bug；
  3. 销毁重建的成本是一次握手，远小于把错误结果返回给业务的成本。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from simconn import (
    DEFAULT_STATEMENT_TIMEOUT_MS,
    ConnectionClosed,
    SimConnection,
)


class ResetError(Exception):
    """归还复位失败。step 字段记录失败在哪一步。"""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"reset step {step!r} failed: {detail}")
        self.step = step
        self.detail = detail


class ConnectionPool:
    """最小可用的连接池（线程安全）。

    参数
    ----
    factory : 建连工厂，默认 SimConnection
    size    : 池上限
    legacy_bug_mode : 复现旧 bug 用——归还时不做任何复位直接放回池。
                      仅 reproduce_bug.py 使用，生产路径永远为 False。
    """

    def __init__(
        self,
        factory: Optional[Callable[[], SimConnection]] = None,
        size: int = 4,
        legacy_bug_mode: bool = False,
    ) -> None:
        self._factory = factory or SimConnection
        self._size = size
        self._legacy_bug_mode = legacy_bug_mode
        self._idle: list[SimConnection] = []
        self._lock = threading.Lock()
        self._next_id = 0
        # 统计：便于测试与运维观测
        self.stats = {
            "created": 0,      # 新建物理连接数
            "reused": 0,       # 复用次数
            "destroyed": 0,    # 因复位失败/已关闭而销毁的连接数
            "reset_failures": 0,
        }

    # ------------------------------------------------------------------ #
    # 复位清单（唯一事实来源；测试会校验清单与实现一致）
    # ------------------------------------------------------------------ #
    # 每项: (步骤名, 复位动作, 校验函数, 期望值)
    # 校验函数读取 conn.snapshot()，返回实际值；不等于期望值即失败。
    RESET_STEPS = (
        ("drain_results",   "drain_results",        "unread_rows",          0),
        ("rollback_txn",    "rollback_if_needed",   "in_transaction",       False),
        ("abort_cleared",   None,                   "txn_aborted",          False),
        ("timeout",         "reset_statement_timeout", "statement_timeout_ms", DEFAULT_STATEMENT_TIMEOUT_MS),
        ("autocommit",      "reset_autocommit",     "autocommit",           True),
        ("session_vars",    "reset_session_vars",   "session_vars",         {}),
        ("prepared_stmts",  "deallocate_all",       "prepared_statements",  set()),
        ("temp_tables",     "drop_temp_tables",     "temp_tables",          set()),
        ("health_check",    "ping",                 "open",                 True),
    )

    def _reset(self, conn: SimConnection) -> None:
        """按清单逐项复位；任一步失败抛 ResetError。"""
        for step, action, key, expected in self.RESET_STEPS:
            try:
                result = getattr(conn, action)() if action is not None else None
            except Exception as exc:
                raise ResetError(step, f"{type(exc).__name__}: {exc}") from exc
            # 健康探测要求显式成功（ping 返回 False 说明探测被吞/失败）
            if step == "health_check" and result is not True:
                raise ResetError(step, "health probe did not succeed")
            actual = conn.snapshot()[key]
            if actual != expected:
                raise ResetError(
                    step, f"state {key!r} is {actual!r}, expected {expected!r}"
                )

    # ------------------------------------------------------------------ #
    # 池操作
    # ------------------------------------------------------------------ #
    def _new_conn(self) -> SimConnection:
        conn = self._factory()
        conn.conn_id = self._next_id
        self._next_id += 1
        self.stats["created"] += 1
        return conn

    def acquire(self) -> SimConnection:
        """借连接。空闲连接先过健康检查，坏的销毁重建。"""
        with self._lock:
            while self._idle:
                conn = self._idle.pop()
                try:
                    alive = conn.ping()
                except ConnectionClosed:
                    self._destroy(conn)
                    continue
                if not alive:
                    self._destroy(conn)
                    continue
                self.stats["reused"] += 1
                return conn
            return self._new_conn()

    def release(self, conn: SimConnection) -> None:
        """归还连接：完整复位；失败或已关闭则销毁，绝不放回。"""
        with self._lock:
            if self._legacy_bug_mode:
                # 复现旧行为：什么都不做直接放回
                self._idle.append(conn)
                return
            if conn.closed:
                self._destroy(conn)
                return
            try:
                self._reset(conn)
            except ResetError:
                self.stats["reset_failures"] += 1
                self._destroy(conn)
                return
            if len(self._idle) < self._size:
                self._idle.append(conn)
            else:
                self._destroy(conn)

    def _destroy(self, conn: SimConnection) -> None:
        try:
            conn.close()
        except Exception:
            pass
        self.stats["destroyed"] += 1

    @contextmanager
    def borrow(self) -> Iterator[SimConnection]:
        """with 用法：无论业务是否抛异常，连接都会被归还/销毁。"""
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release(conn)

    def shutdown(self) -> None:
        with self._lock:
            for conn in self._idle:
                self._destroy(conn)
            self._idle.clear()

    @property
    def idle_count(self) -> int:
        return len(self._idle)


__all__ = ["ConnectionPool", "ResetError"]
