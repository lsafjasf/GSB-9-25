"""crosscheck.py — 对拍：修复后“复用连接”与“全新连接”行为必须一致。

对每种工作负载 W：
  1. 新连接 baseline：新建连接直接跑 probe，记录行为签名 S_new；
  2. 复用连接：借连接 -> 跑 W（弄脏）-> 归还（触发复位）-> 再借 -> 跑 probe，
     记录行为签名 S_reused；
  3. 断言 S_new == S_reused，且复用连接借出时快照 == 新连接基准快照。

运行：python3 crosscheck.py   （全部一致退出码 0，否则 1）
"""

from __future__ import annotations

import sys
from typing import Any, Callable

from pool import ConnectionPool
from simconn import InFailedTransaction, SimConnection


def probe(conn: SimConnection) -> dict[str, Any]:
    """探针：在连接上执行固定操作序列，返回行为签名。"""
    sig: dict[str, Any] = {}
    sig["snapshot"] = conn.snapshot()
    rows = conn.execute_query("SELECT 1", rows=[("ok",)])
    sig["query_returned"] = rows
    sig["fetchall"] = conn.fetchall()          # 应只含本次查询的行
    conn.set_autocommit(False)
    conn.begin()
    conn.execute_write("INSERT INTO t VALUES (1)")
    conn.rollback()
    sig["txn_cycle"] = "ok"
    conn.set_autocommit(True)
    sig["final_snapshot"] = conn.snapshot()
    return sig


# ---- 工作负载：每种都弄脏一类（或多类）状态 ----

def wl_clean(conn: SimConnection) -> None:
    conn.execute_query("SELECT 1", rows=[(1,)])
    conn.fetchall()


def wl_uncommitted_txn(conn: SimConnection) -> None:
    conn.set_autocommit(False)
    conn.begin()
    conn.execute_write("INSERT INTO t VALUES (1)")
    # 故意不 commit/rollback


def wl_aborted_txn(conn: SimConnection) -> None:
    conn.set_autocommit(False)
    conn.begin()
    try:
        conn.execute_write("SELECT BROKEN")  # 事务进入 aborted
    except InFailedTransaction:
        pass


def wl_timeout_and_vars(conn: SimConnection) -> None:
    conn.set_statement_timeout(3000)
    conn.set_session_var("application_name", "wl")
    conn.set_session_var("search_path", "secret_schema")


def wl_unread_results(conn: SimConnection) -> None:
    conn.execute_query("SELECT * FROM t", rows=[(i,) for i in range(50)])
    # 故意不 fetchall


def wl_prepared_and_temp(conn: SimConnection) -> None:
    conn.prepare("wl_stmt")
    conn.execute_write("CREATE TEMP TABLE wl_tmp (id int)")


def wl_everything(conn: SimConnection) -> None:
    wl_uncommitted_txn(conn)
    conn.rollback()
    wl_timeout_and_vars(conn)
    wl_unread_results(conn)
    wl_prepared_and_temp(conn)


WORKLOADS: list[tuple[str, Callable[[SimConnection], None]]] = [
    ("clean", wl_clean),
    ("uncommitted_txn", wl_uncommitted_txn),
    ("aborted_txn", wl_aborted_txn),
    ("timeout_and_vars", wl_timeout_and_vars),
    ("unread_results", wl_unread_results),
    ("prepared_and_temp", wl_prepared_and_temp),
    ("everything", wl_everything),
]


def main() -> int:
    baseline_sig = probe(SimConnection())
    baseline_snap = SimConnection.fresh_snapshot()
    failures = 0

    print(f"{'workload':22s} {'snapshot==fresh':16s} {'behavior==new':14s} result")
    for name, workload in WORKLOADS:
        pool = ConnectionPool()
        conn = pool.acquire()
        workload(conn)
        pool.release(conn)

        reused = pool.acquire()
        snap_ok = reused.snapshot() == baseline_snap
        sig = probe(reused)
        behavior_ok = sig == baseline_sig
        ok = snap_ok and behavior_ok
        failures += 0 if ok else 1
        print(f"{name:22s} {str(snap_ok):16s} {str(behavior_ok):14s} {'PASS' if ok else 'FAIL'}")
        pool.shutdown()

    print(f"\n对拍结果: {len(WORKLOADS) - failures}/{len(WORKLOADS)} 通过")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
