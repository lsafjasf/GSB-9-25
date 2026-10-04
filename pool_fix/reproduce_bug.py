"""reproduce_bug.py — 复现旧 bug：归还连接不复位，残留状态泄漏给下一个使用者。

运行：python3 reproduce_bug.py
退出码：0 = 成功复现 bug（旧行为确实泄漏）；1 = 未复现。

场景：使用者 A 借出连接，留下未提交事务、语句超时、未读结果、
会话变量、预编译语句、临时表，然后归还。旧池（legacy_bug_mode）
直接放回。使用者 B 借到**同一条物理连接**，看到的全是 A 的残留。
"""

from __future__ import annotations

import sys

from pool import ConnectionPool
from simconn import SimConnection


def dirty_usage(conn: SimConnection) -> None:
    """使用者 A：把能弄脏的状态全部弄脏。"""
    conn.set_autocommit(False)
    conn.begin()
    conn.execute_write("INSERT INTO t VALUES (1)")   # 未提交事务
    conn.set_statement_timeout(5000)                 # 语句超时
    conn.execute_query("SELECT * FROM big", rows=[(i,) for i in range(100)])
    conn.set_session_var("application_name", "tenant-A")
    conn.prepare("stmt_a")
    conn.execute_write("CREATE TEMP TABLE tmp_a (id int)")


def main() -> int:
    pool = ConnectionPool(legacy_bug_mode=True)

    conn_a = pool.acquire()
    dirty_usage(conn_a)
    dirty_snapshot = conn_a.snapshot()
    pool.release(conn_a)  # 旧行为：不复位，直接放回池

    conn_b = pool.acquire()
    same_physical = conn_b is conn_a
    residue = {
        k: v for k, v in conn_b.snapshot().items()
        if v != SimConnection.fresh_snapshot()[k]
    }

    print("== 旧行为复现（归还时不复位）==")
    print(f"使用者 B 拿到的是同一条物理连接: {same_physical}")
    print("使用者 B 观察到的残留状态:")
    for key, value in residue.items():
        print(f"  {key:22s} = {value!r}   (新连接应为 {SimConnection.fresh_snapshot()[key]!r})")

    # 业务后果示例：B 以为自己在 autocommit 模式，实际写进了 A 的事务里
    leaked = bool(residue)
    print(f"\n结论: {'BUG 复现成功 —— 状态泄漏' if leaked and same_physical else '未复现'}")

    # 对照：修复后的池
    print("\n== 修复后对照（同一脚本、同一脏用法）==")
    fixed = ConnectionPool()
    c1 = fixed.acquire()
    dirty_usage(c1)
    fixed.release(c1)
    c2 = fixed.acquire()
    residue_fixed = {
        k: v for k, v in c2.snapshot().items()
        if v != SimConnection.fresh_snapshot()[k]
    }
    print(f"同一连接经完整复位后被复用: {c2 is c1}（未销毁，快照==新连接）")
    print(f"使用者 B 观察到的残留: {residue_fixed or '无（与新连接一致）'}")
    print(f"统计: {fixed.stats}")

    return 0 if leaked and same_physical and not residue_fixed else 1


if __name__ == "__main__":
    sys.exit(main())
