# -*- coding: utf-8 -*-
"""对拍：复用连接 vs 全新连接，行为必须逐字节一致。

对每个「弄脏」场景：先用脏操作污染一条池连接并归还（触发复位或销毁），
再从池中借出执行固定探针序列；同时用一条全新连接执行同样的探针序列。
两边输出必须完全相同，并与 golden_diff_data.txt 一致。

运行：
    python3 diff_check.py            # 与 golden 数据比对
    python3 diff_check.py --write    # 重新生成 golden 数据
"""

from __future__ import annotations

import sys
import time

from connection import Connection, ReadTimeoutError
from pool import ConnectionPool
from server import Server

GOLDEN = "golden_diff_data.txt"


def dirty_clean(conn):
    pass


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
    dirty_timeout(conn)
    time.sleep(0.35)


def dirty_all(conn):
    dirty_timeout(conn)
    time.sleep(0.35)
    conn.multi()
    conn.set("dirty-all", "x")


def dirty_killed(conn):
    conn.kill()
    time.sleep(0.1)


SCENARIOS = [
    ("clean", dirty_clean),
    ("timeout", dirty_timeout),
    ("transaction", dirty_transaction),
    ("buffer", dirty_buffer),
    ("all(timeout+tx+buffer)", dirty_all),
    ("killed", dirty_killed),
]


def probe(conn):
    """固定探针序列：任何连接执行结果都必须一致。"""
    out = []
    out.append("ping=%s" % conn.ping())
    out.append("tx=%s" % conn.status()["TX"])
    out.append("unread=%d" % conn.unread_bytes())
    out.append("timeout=%s" % conn.sock.gettimeout())
    out.append("set=%s" % conn.set("probe", "v1"))
    out.append("get=%s" % conn.get("probe"))
    out.append("multi=%s" % conn.multi())
    out.append("set2=%s" % conn.set("probe", "v2"))
    out.append("rollback=%s" % conn.rollback())
    out.append("get2=%s" % conn.get("probe"))
    out.append("tx2=%s" % conn.status()["TX"])
    return out


def main():
    write = "--write" in sys.argv
    lines = []
    ok = True
    with Server() as srv:
        host, port = srv.server_address
        pool = ConnectionPool(host, port, maxsize=2)
        try:
            for name, dirty in SCENARIOS:
                with pool.lease() as conn:
                    dirty(conn)
                with pool.lease() as reused:
                    reused_out = probe(reused)
                fresh = Connection(host, port)
                try:
                    fresh_out = probe(fresh)
                finally:
                    fresh.close()
                same = reused_out == fresh_out
                ok = ok and same
                lines.append("== scenario: %s" % name)
                lines.append("reused == fresh: %s" % same)
                for i, (a, b) in enumerate(zip(reused_out, fresh_out)):
                    mark = "" if a == b else "   <<< MISMATCH"
                    lines.append("  probe[%d] reused=%-18s fresh=%-18s%s"
                                 % (i, a, b, mark))
                lines.append("")
            lines.append("pool: created=%d destroyed=%d reset_ok=%d reset_failed=%d"
                         % (pool.created, pool.destroyed, pool.reset_ok,
                            pool.reset_failed))
        finally:
            pool.close()
    text = "\n".join(lines) + "\n"
    print(text)
    if write:
        with open(GOLDEN, "w", encoding="utf-8") as f:
            f.write(text)
        print("golden data written to", GOLDEN)
        return
    try:
        with open(GOLDEN, encoding="utf-8") as f:
            golden = f.read()
    except FileNotFoundError:
        print("golden data missing; run: python3 diff_check.py --write")
        sys.exit(2)
    # 最后一行是池指标（创建数等），受场景顺序影响但与行为无关，也纳入比对
    if text != golden:
        print("DIFFERS from golden data in", GOLDEN)
        sys.exit(1)
    if not ok:
        print("reused connection behavior differs from fresh connection")
        sys.exit(1)
    print("OK: reused connections behave identically to fresh connections "
          "and match golden data.")


if __name__ == "__main__":
    main()
