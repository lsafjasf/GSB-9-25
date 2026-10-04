"""simconn.py — 一个仅依赖标准库的“类数据库连接”模拟器。

为什么不用真数据库：本任务的关注点是**连接池归还时的状态复位策略**，
而非某种具体数据库协议。本模拟器把真实连接（psycopg/MySQL Connector/
JDBC DataSource 等）归还时最常见的残留状态全部显式建模：

    1. 未结束的事务（autocommit=False 且有未提交修改）
    2. 语句超时 / 会话级 GUC（statement_timeout 一类的会话变量）
    3. 服务端未读结果集（result buffer 里还挂着行）
    4. 调用方临时改过的 autocommit 开关
    5. 会话变量（SET application_name / SET x = ...）
    6. 服务端预编译语句（PREPARE / DEALLOCATE）
    7. 临时表（session 级，随连接物理存活）
    8. 连接被服务端强制关闭（RST / kill）

每个方法都能通过 ``fail_at`` 抛错，以模拟“复位动作本身失败”；
``force_close`` 模拟网络中断/服务端 kill。

错误语义刻意模仿真实驱动：在关闭的连接上操作抛 ConnectionClosed，
复位过程中任何一步报错，连接都不应再被信任（可能已经半坏）。
"""

from __future__ import annotations

from typing import Any, Optional


class ConnectionError_(Exception):
    """模拟驱动的 InterfaceError/OperationalError 基类。"""


class ConnectionClosed(ConnectionError_):
    """连接已关闭（本地关闭或被服务端强制关闭）。"""


class InFailedTransaction(ConnectionError_):
    """模拟 PostgreSQL 的 InFailedSqlTransaction：

    事务处于 aborted 状态，除 ROLLBACK 外什么命令都收不了。
    """


DEFAULT_STATEMENT_TIMEOUT_MS = 0  # 0 = 无超时，连接出厂状态


class SimConnection:
    """模拟一条物理数据库连接。

    连接出厂（``__init__``）时的状态就是“新连接基准状态”，
    也是复位成功后必须回到的状态。
    """

    #: 复位清单/测试统一从这里取“可观察状态”的键
    SNAPSHOT_KEYS = (
        "open",
        "healthy",
        "autocommit",
        "in_transaction",
        "txn_aborted",
        "statement_timeout_ms",
        "unread_rows",
        "session_vars",
        "prepared_statements",
        "temp_tables",
    )

    def __init__(self, conn_id: int = 0) -> None:
        self.conn_id = conn_id
        self._open = True
        self._healthy = True
        self._autocommit = True
        self._in_transaction = False
        self._txn_aborted = False
        self._dirty_in_txn = False  # 事务里是否真的发生过修改
        self._statement_timeout_ms = DEFAULT_STATEMENT_TIMEOUT_MS
        self._result_buffer: list[tuple] = []
        self._session_vars: dict[str, str] = {}
        self._prepared: set[str] = set()
        self._temp_tables: set[str] = set()
        # 故障注入：动作名 -> "raise" | "noop"
        # raise: 该动作抛 ResetActionFailed
        # noop : 该动作静默不生效（模拟复位命令被吞 / 复位后状态没变）
        self.fail_at: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 内部机制
    # ------------------------------------------------------------------ #
    def _ensure_live(self) -> None:
        if not self._open:
            raise ConnectionClosed(f"connection {self.conn_id} is closed")
        if not self._healthy:
            raise ConnectionClosed(
                f"connection {self.conn_id} was force-closed by server"
            )

    def _do(self, action: str) -> bool:
        """故障注入点：所有“会产生协议往返”的动作都走这里。

        返回 True 表示动作真正执行；False 表示 noop 注入——命令被吞掉，
        调用方不得改动任何本地状态（模拟服务端实际没执行复位）。
        """
        mode = self.fail_at.get(action)
        if mode == "raise":
            raise ResetActionFailed(f"action {action!r} failed on conn {self.conn_id}")
        if mode == "noop":
            return False
        return True

    # ------------------------------------------------------------------ #
    # 业务侧使用的 API（模拟 DB-API 2.0 风格）
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        self._ensure_live()
        if self._autocommit:
            raise ConnectionError_("cannot BEGIN while autocommit is on")
        if self._in_transaction:
            raise ConnectionError_("already in a transaction")
        if not self._do("begin"):
            return
        self._in_transaction = True
        self._txn_aborted = False
        self._dirty_in_txn = False

    def commit(self) -> None:
        self._ensure_live()
        if not self._do("commit"):
            return
        self._in_transaction = False
        self._txn_aborted = False
        self._dirty_in_txn = False

    def rollback(self) -> None:
        self._ensure_live()
        # ROLLBACK 是 aborted 事务里唯一允许的命令
        if not self._do("rollback"):
            return
        self._in_transaction = False
        self._txn_aborted = False
        self._dirty_in_txn = False

    def execute_write(self, sql: str) -> None:
        """模拟一条写操作（INSERT/UPDATE/DDL），可能把事务推入 aborted。"""
        self._ensure_live()
        self._guard_aborted()
        if sql.upper() == "SELECT BROKEN":
            # 模拟一条让事务进入 aborted 状态的失败语句
            self._txn_aborted = True
            raise InFailedTransaction("statement failed, transaction is aborted")
        if not self._autocommit:
            self._in_transaction = True
            self._dirty_in_txn = True
        if sql.upper().startswith("CREATE TEMP TABLE "):
            name = sql.split(None, 3)[3].split(None, 1)[0].strip()
            self._temp_tables.add(name)
        if sql.upper().startswith("PREPARE "):
            name = sql.split(None, 2)[1].strip()
            self._prepared.add(name)

    def execute_query(self, sql: str, rows: Optional[list[tuple]] = None) -> list[tuple]:
        """模拟返回结果集的查询，行停留在缓冲里直到 fetchall。"""
        self._ensure_live()
        self._guard_aborted()
        rows = rows or [(1, "a"), (2, "b")]
        self._result_buffer.extend(rows)
        return list(rows)

    def fetchall(self) -> list[tuple]:
        self._ensure_live()
        self._guard_aborted()
        rows = self._result_buffer
        self._result_buffer = []
        return rows

    def set_session_var(self, name: str, value: Any) -> None:
        self._ensure_live()
        self._guard_aborted()
        self._session_vars[name] = str(value)

    def reset_session_var(self, name: str) -> None:
        self._ensure_live()
        self._guard_aborted()
        self._session_vars.pop(name, None)

    def set_autocommit(self, on: bool) -> None:
        self._ensure_live()
        self._autocommit = bool(on)

    def set_statement_timeout(self, ms: int) -> None:
        self._ensure_live()
        self._guard_aborted()
        self._statement_timeout_ms = int(ms)

    def prepare(self, name: str) -> None:
        self._ensure_live()
        self._guard_aborted()
        self._prepared.add(name)

    def deallocate_all(self) -> None:
        self._ensure_live()
        # 复位专用：即使事务 aborted 也允许，模拟真实驱动的 DEALLOCATE ALL
        if not self._do("deallocate_all"):
            return
        self._prepared.clear()

    def drop_temp_tables(self) -> None:
        self._ensure_live()
        if not self._do("drop_temp_tables"):
            return
        self._temp_tables.clear()

    def drain_results(self) -> int:
        """排空未读结果集，返回丢弃的行数。"""
        self._ensure_live()
        if not self._do("drain_results"):
            return 0
        n = len(self._result_buffer)
        self._result_buffer = []
        return n

    def rollback_if_needed(self) -> bool:
        """复位专用：有打开/aborted 的事务就 ROLLBACK，返回是否执行了回滚。"""
        self._ensure_live()
        if self._in_transaction or self._txn_aborted:
            if not self._do("rollback"):
                return False
            self._in_transaction = False
            self._txn_aborted = False
            self._dirty_in_txn = False
            return True
        return False

    def reset_statement_timeout(self) -> None:
        self._ensure_live()
        if not self._do("reset_timeout"):
            return
        self._statement_timeout_ms = DEFAULT_STATEMENT_TIMEOUT_MS

    def reset_autocommit(self) -> None:
        self._ensure_live()
        if not self._do("reset_autocommit"):
            return
        self._autocommit = True

    def reset_session_vars(self) -> None:
        self._ensure_live()
        if not self._do("reset_session_vars"):
            return
        self._session_vars = {}

    def ping(self) -> bool:
        """健康探测：SELECT 1。坏连接抛错；noop 注入时返回 False。"""
        self._ensure_live()
        return self._do("ping")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def force_close(self) -> None:
        """模拟服务端 kill / 网络 RST：连接直接变僵尸。"""
        self._healthy = False
        self._open = False

    def close(self) -> None:
        """正常关闭（销毁路径调用）。"""
        self._open = False
        self._healthy = False

    # ------------------------------------------------------------------ #
    # 观察接口
    # ------------------------------------------------------------------ #
    @property
    def closed(self) -> bool:
        return (not self._open) or (not self._healthy)

    def snapshot(self) -> dict[str, Any]:
        """当前全部可观察状态。测试与对拍都用它做断言。"""
        return {
            "open": self._open and self._healthy,
            "healthy": self._healthy,
            "autocommit": self._autocommit,
            "in_transaction": self._in_transaction,
            "txn_aborted": self._txn_aborted,
            "statement_timeout_ms": self._statement_timeout_ms,
            "unread_rows": len(self._result_buffer),
            "session_vars": dict(sorted(self._session_vars.items())),
            "prepared_statements": set(self._prepared),
            "temp_tables": set(self._temp_tables),
        }

    @classmethod
    def fresh_snapshot(cls) -> dict[str, Any]:
        """一条全新连接（未被任何业务碰过）的基准快照。"""
        return cls().snapshot()

    def _guard_aborted(self) -> None:
        if self._txn_aborted:
            raise InFailedTransaction(
                "current transaction is aborted; commands ignored until ROLLBACK"
            )


class ResetActionFailed(Exception):
    """复位动作执行失败（模拟网络错误 / 服务端报错）。"""


__all__ = [
    "SimConnection",
    "ResetActionFailed",
    "ConnectionClosed",
    "InFailedTransaction",
    "DEFAULT_STATEMENT_TIMEOUT_MS",
]
