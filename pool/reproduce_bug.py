# -*- coding: utf-8 -*-
"""复现：连接归还后残留超时 / 事务 / 缓冲，下一个使用者收到莫名结果。

前半段用「直接放回、不做任何复位」的朴素连接池演示四类残留；
后半段用修复后的 ConnectionPool（归还时逐项复位）对照。

运行：python3 reproduce_bug.py
"""

from __future__ import annotations

import time

from connection import Connection, ReadTimeoutError
from pool import ConnectionPool
from server import Server


class NaivePool:
    """反面教材：归还时不复位，直接塞回队列。"""

    def __init__(self, host, port, maxsize=2):
        self.host, self.port, self.maxsize = host, port, maxsize
        self.idle = []
        self.n = 0

    def acquire(self):
        if self.idle:
            return self.idle.pop()
        self.n += 1
        return Connection(self.host, self.port)

    def release(self, conn):
        self.idle.append(conn)


def case_timeout_leak(pool_get, label):
    """使用者把超时调到很短且没恢复，下一个人正常请求莫名超时。"""
    p = pool_get()
    c = p.acquire()
    try:
        c.set_timeout(0.05)
        try:
            c.sleep(200)
        except ReadTimeoutError:
            pass  # 超时后使用者直接归还
    finally:
        p.release(c)

    c2 = p.acquire()
    t0 = time.monotonic()
    try:
        c2.ping()
        print("[%s] 超时残留: 下一个 PING 成功（无残留）" % label)
    except ReadTimeoutError:
        print("[%s] 超时残留: 下一个 PING 莫名 ReadTimeoutError（%.2fs 后）  <<< 泄漏"
              % (label, time.monotonic() - t0))
    finally:
        # 朴素池这条连接缓冲里还有迟到 SLEPT，避免影响服务器统计，直接关闭
        c2.close() if hasattr(c2, "close") else None
        p.release(c2)


def case_transaction_leak(pool_get, label):
    """MULTI 后抛异常、没 ROLLBACK，下一个人发现自己在别人的事务里。"""
    p = pool_get()
    c = p.acquire()
    try:
        c.multi()
        c.set("k", "v")
        raise RuntimeError("boom: 使用者中途异常退出")
    except RuntimeError:
        pass
    finally:
        p.release(c)

    c2 = p.acquire()
    try:
        tx = c2.status().get("TX")
        if tx == "1":
            print("[%s] 事务残留: 新使用者 STATUS 得到 TX=1（继承了别人的事务）  <<< 泄漏" % label)
        else:
            print("[%s] 事务残留: 新使用者 STATUS 得到 TX=0（无残留）" % label)
    finally:
        c2.close() if hasattr(c2, "close") else None
        p.release(c2)


def case_buffer_leak(pool_get, label):
    """超时后迟到的 SLEPT 留在缓冲，下一个人读到上一次的响应。"""
    p = pool_get()
    c = p.acquire()
    try:
        c.set_timeout(0.05)
        try:
            c.sleep(200)
        except ReadTimeoutError:
            pass
    finally:
        p.release(c)
    time.sleep(0.3)  # 等迟到响应到达

    c2 = p.acquire()
    try:
        reply = c2.ping()
        if reply == "PONG":
            print("[%s] 缓冲残留: PING -> PONG（无残留）" % label)
        else:
            print("[%s] 缓冲残留: PING -> %r（读到了上次的响应，命令-响应错位）  <<< 泄漏"
                  % (label, reply))
    finally:
        c2.close() if hasattr(c2, "close") else None
        p.release(c2)


def case_killed_leak(pool_get, label):
    """连接被服务端强制关闭后归还，下一个人一用就炸。"""
    p = pool_get()
    c = p.acquire()
    try:
        c.kill()
        time.sleep(0.1)
    finally:
        p.release(c)

    c2 = p.acquire()
    try:
        try:
            c2.ping()
            print("[%s] 强制关闭: 下一个 PING 成功（无残留）" % label)
        except Exception as exc:
            print("[%s] 强制关闭: 下一个 PING 抛 %s  <<< 泄漏/失效连接被复用"
                  % (label, type(exc).__name__))
    finally:
        c2.close() if hasattr(c2, "close") else None
        p.release(c2)


def main():
    with Server() as srv:
        host, port = srv.server_address

        print("=== 修复前：朴素池（归还不复位）===")
        case_timeout_leak(lambda: NaivePool(host, port), "朴素")
        case_transaction_leak(lambda: NaivePool(host, port), "朴素")
        case_buffer_leak(lambda: NaivePool(host, port), "朴素")
        case_killed_leak(lambda: NaivePool(host, port), "朴素")

        print()
        print("=== 修复后：ConnectionPool（归还时逐项复位，失败销毁）===")
        cases = [case_timeout_leak, case_transaction_leak,
                 case_buffer_leak, case_killed_leak]
        for case in cases:
            p = ConnectionPool(host, port, maxsize=2, timeout=5.0)
            try:
                case(lambda: p, "修复")
            finally:
                p.close()


if __name__ == "__main__":
    main()
