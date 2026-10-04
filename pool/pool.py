# -*- coding: utf-8 -*-
"""ConnectionPool —— 归还时逐项复位、复位失败即销毁的连接池。

归还语义（release）：

  1. 连接已关闭（对端 KILL、网络断开）→ 直接销毁，不进池。
  2. 否则调用 conn.reset() 逐项复位（见 connection.RESET_STEPS）。
  3. reset() 抛 ResetError（任何一项复位失败）→ 销毁连接，不进池。
     判断依据：复位失败的连接无法保证「与新连接不可区分」，放回池中
     会把残留状态泄漏给下一个使用者；销毁并懒创建新连接是唯一安全选择。
  4. 复位成功 → 放回空闲队列，可被下一个使用者复用。

使用方在 with 块里抛异常不影响归还：__exit__ 无条件走同一条
release 路径（先复位，失败销毁），异常原样向上传播。
"""

from __future__ import annotations

import queue
import threading
from contextlib import contextmanager

from connection import Connection, ResetError


class PoolClosedError(Exception):
    """池已关闭。"""


class ConnectionPool:
    def __init__(self, host: str, port: int, maxsize: int = 4,
                 timeout: float = 5.0, acquire_timeout: float = 10.0,
                 conn_factory=None):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.host = host
        self.port = port
        self.maxsize = maxsize
        self.timeout = timeout
        self.acquire_timeout = acquire_timeout
        self._factory = conn_factory or (
            lambda: Connection(host, port, timeout=timeout))
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._lock = threading.Lock()
        self._total = 0          # 当前存活连接数（空闲 + 借出）
        self._closed = False
        # 观测指标（对拍与回归测试用）
        self.created = 0         # 累计创建
        self.destroyed = 0       # 累计销毁（复位失败 / 已死连接）
        self.reset_ok = 0        # 复位成功并放回池中
        self.reset_failed = 0    # 复位失败被销毁

    # ------------------------------------------------------------ 借出

    def acquire(self) -> Connection:
        if self._closed:
            raise PoolClosedError("pool is closed")
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._total < self.maxsize:
                self._total += 1
                try:
                    conn = self._factory()
                except Exception:
                    with self._lock:
                        self._total -= 1
                    raise
                self.created += 1
                return conn
        try:
            return self._idle.get(timeout=self.acquire_timeout)
        except queue.Empty:
            raise TimeoutError("no connection available within %.1fs"
                               % self.acquire_timeout)

    # ------------------------------------------------------------ 归还

    def release(self, conn: Connection) -> None:
        """归还连接：先逐项复位，任何一步失败都销毁而不是放回池中。"""
        if self._closed or conn.closed:
            self._discard(conn)
            return
        try:
            conn.reset()
        except ResetError:
            self.reset_failed += 1
            self._discard(conn)
            return
        except Exception:
            # 未知异常同样视为复位失败：宁可销毁，不可复用
            self.reset_failed += 1
            self._discard(conn)
            return
        self.reset_ok += 1
        self._idle.put(conn)

    def _discard(self, conn: Connection) -> None:
        try:
            conn.close()
        finally:
            self.destroyed += 1
            with self._lock:
                self._total -= 1

    # ------------------------------------------------------------ 上下文

    @contextmanager
    def lease(self):
        """with pool.lease() as conn: ... —— 异常时也保证走 release。"""
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release(conn)

    # ------------------------------------------------------------ 关闭

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            conn.close()
            with self._lock:
                self._total -= 1

    @property
    def idle(self) -> int:
        return self._idle.qsize()

    @property
    def total(self) -> int:
        with self._lock:
            return self._total
