# -*- coding: utf-8 -*-
"""Connection —— kvserver 的客户端连接，以及归还连接池前的逐项状态复位。

连接上可能残留的状态（每一项都有对应的可观察测试，见 test_pool.py）：

  #  状态                残留来源                                下一个使用者观察到的现象
  -  ------------------  -------------------------------------  -------------------------------
  1  socket 超时/阻塞模式 使用者 settimeout(0.05) 后没恢复        正常请求莫名抛 ReadTimeoutError
  2  服务端事务           MULTI 之后异常退出，没 ROLLBACK         自己的 SET 被一起提交/丢弃，
                                                               EXEC 意外 COMMITTED
  3  客户端读缓冲         上次请求超时，迟到的响应还在 socket 里   读到上次的响应（命令-响应错位）
  4  待发送字节           上次写入中途失败                        复位时强制 flush，失败即销毁

复位顺序（reset() 严格按此执行，任何一步失败都抛 ResetError，
由连接池销毁连接而不是放回池中）：

  1. liveness   —— 对端已关闭/半关闭的连接直接判死
  2. timeout    —— 恢复默认超时与阻塞模式
  3. flush      —— 把待发送字节全部发出去（补发后服务端会产生对应响应）
  4. drain      —— 用 select 探测并丢弃读缓冲里的残留字节
                   （超时迟到的响应、flush 引发的响应都在这里清掉）
  5. transaction—— 查 STATUS，若 TX=1 则 ROLLBACK 并确认 TX=0
  6. ping       —— 最后一道端到端校验：PING 必须恰好收到 PONG
"""

from __future__ import annotations

import select
import socket
from dataclasses import dataclass, field


class ConnectionError_(Exception):
    """连接级错误（对端关闭、写失败等）。"""


class ReadTimeoutError(ConnectionError_):
    """读响应超时。"""


class ProtocolError(ConnectionError_):
    """服务端响应不符合协议。"""


class ResetError(Exception):
    """某一项复位失败；携带失败步骤名与原因。连接必须被销毁。"""

    def __init__(self, step: str, reason: str):
        super().__init__("reset step %r failed: %s" % (step, reason))
        self.step = step
        self.reason = reason


DEFAULT_TIMEOUT = 5.0

# reset() 的复位步骤顺序，对外公开以便测试与文档核对。
RESET_STEPS = ("liveness", "timeout", "flush", "drain", "transaction", "ping")


@dataclass
class ResetReport:
    """一次复位的审计记录：每一步做了什么。"""

    steps: list = field(default_factory=list)   # 已完成的步骤名，按序
    rolled_back: bool = False                   # 是否回滚了残留事务
    drained_bytes: int = 0                      # 丢弃的残留读缓冲字节数
    flushed_bytes: int = 0                      # 补发的待发送字节数
    restored_timeout: bool = False              # 是否恢复过 socket 超时


class Connection:
    """一条到 kvserver 的 TCP 连接。

    使用方约定：命令-响应一一对应（不流水线）。reset() 把连接恢复到
    与新连接不可区分的状态；任何一步失败都抛 ResetError。
    """

    def __init__(self, host: str, port: int, timeout: float = DEFAULT_TIMEOUT,
                 drain_grace: float = 0.25):
        self.host = host
        self.port = port
        self.default_timeout = timeout
        self.drain_grace = drain_grace
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._rbuf = b""        # 客户端读缓冲（已 recv 但还没被消费的）
        self._wbuf = b""        # 待发送缓冲（写失败时残留，复位时补发）
        self._timed_out = False # 本次借出期间是否发生过读超时（迟到响应可能还在路上）
        self._closed = False

    # ------------------------------------------------------------ 底层 IO

    def _recv_more(self) -> None:
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            self._timed_out = True
            raise ReadTimeoutError("timed out waiting for response")
        except OSError as exc:
            raise ConnectionError_("recv failed: %s" % exc)
        if not chunk:
            raise ConnectionError_("server closed the connection")
        self._rbuf += chunk

    def _readline(self) -> str:
        while b"\n" not in self._rbuf:
            self._recv_more()
        line, self._rbuf = self._rbuf.split(b"\n", 1)
        return line.rstrip(b"\r").decode("utf-8")

    def _sendall(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError as exc:
            # 发送失败：剩余字节进入待发送缓冲，复位时再处理
            self._wbuf += data
            raise ConnectionError_("send failed: %s" % exc)

    def _cmd(self, line: str) -> str:
        self._ensure_open()
        self._sendall((line + "\n").encode("utf-8"))
        return self._readline()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConnectionError_("connection is closed")

    # ------------------------------------------------------------ 协议方法

    def ping(self) -> str:
        return self._cmd("PING")

    def get(self, key: str) -> str | None:
        resp = self._cmd("GET " + key)
        if resp == "NULL":
            return None
        if resp.startswith("VALUE "):
            return resp[len("VALUE "):]
        raise ProtocolError("unexpected GET response: %r" % resp)

    def set(self, key: str, value: str) -> str:
        return self._cmd("SET %s %s" % (key, value))

    def multi(self) -> str:
        return self._cmd("MULTI")

    def exec(self) -> str:
        return self._cmd("EXEC")

    def rollback(self) -> str:
        return self._cmd("ROLLBACK")

    def sleep(self, ms: int) -> str:
        return self._cmd("SLEEP %d" % ms)

    def status(self) -> dict:
        resp = self._cmd("STATUS")
        out = {}
        for field_ in resp.split():
            k, _, v = field_.partition("=")
            out[k] = v
        return out

    def kill(self) -> None:
        """让服务端直接关闭本连接（模拟对端强制断开）。"""
        try:
            self._sendall(b"KILL\n")
        except ConnectionError_:
            pass

    # ------------------------------------------------------------ 状态操作

    def set_timeout(self, seconds: float | None) -> None:
        """修改 socket 超时（使用者可以调，但归还前必须恢复）。"""
        self._ensure_open()
        self.sock.settimeout(seconds)

    def unread_bytes(self) -> int:
        """读缓冲里残留的、还没被消费的响应字节数（含内核缓冲）。"""
        n = len(self._rbuf)
        if self._closed:
            return n
        try:
            ready, _, _ = select.select([self.sock], [], [], 0)
        except (OSError, ValueError):
            return n
        if ready:
            try:
                chunk = self.sock.recv(65536, socket.MSG_PEEK)
                n += len(chunk)
            except (BlockingIOError, OSError):
                pass
        return n

    # ------------------------------------------------------------ 复位

    def reset(self) -> ResetReport:
        """逐项复位，返回审计记录；任何一步失败抛 ResetError。

        判断依据：复位后连接必须与新连接不可区分。任何一步无法确认
        达到这一点（例如对端不响应、ROLLBACK 失败、缓冲清不干净），
        继续复用就会把残留状态泄漏给下一个使用者，因此抛 ResetError，
        由连接池销毁该连接。
        """
        report = ResetReport()
        self._step_liveness(report)
        self._step_timeout(report)
        self._step_flush(report)
        self._step_drain(report)
        self._step_transaction(report)
        self._step_ping(report)
        return report

    def _step_liveness(self, report: ResetReport) -> None:
        if self._closed:
            raise ResetError("liveness", "connection already closed")
        try:
            # 先 select 探测（socket 带超时时 MSG_DONTWAIT 并不可靠，
            # Python 仍会先按超时等待），可读再 MSG_PEEK 看一眼：
            # 读到 b"" 说明对端已关闭；读到数据说明连接活着（残留
            # 数据由后面的 drain 步骤处理）。
            ready, _, _ = select.select([self.sock], [], [], 0)
            if ready:
                data = self.sock.recv(1, socket.MSG_PEEK)
                if data == b"":
                    raise ResetError("liveness", "peer has closed the connection")
        except ResetError:
            raise
        except OSError as exc:
            raise ResetError("liveness", "socket error: %s" % exc)
        report.steps.append("liveness")

    def _step_timeout(self, report: ResetReport) -> None:
        try:
            current = self.sock.gettimeout()
            if current != self.default_timeout:
                self.sock.settimeout(self.default_timeout)
                report.restored_timeout = True
        except OSError as exc:
            raise ResetError("timeout", "cannot restore timeout: %s" % exc)
        report.steps.append("timeout")

    def _step_flush(self, report: ResetReport) -> None:
        if self._wbuf:
            pending, self._wbuf = self._wbuf, b""
            try:
                self.sock.sendall(pending)
            except OSError as exc:
                raise ResetError("flush", "cannot flush %d pending bytes: %s"
                                 % (len(pending), exc))
            report.flushed_bytes = len(pending)
        report.steps.append("flush")

    def _step_transaction(self, report: ResetReport) -> None:
        # 缓冲已清空，此时 STATUS 的响应必须严格是 TX=0/1；
        # 收到别的内容说明命令-响应错位，连接不可信。
        try:
            st = self.status()
        except ConnectionError_ as exc:
            raise ResetError("transaction", "STATUS failed: %s" % exc)
        if st.get("TX") not in ("0", "1"):
            raise ResetError("transaction", "garbled STATUS response: %r" % st)
        if st["TX"] == "1":
            try:
                reply = self.rollback()
            except ConnectionError_ as exc:
                raise ResetError("transaction", "ROLLBACK failed: %s" % exc)
            if reply != "ROLLED BACK":
                raise ResetError("transaction", "unexpected ROLLBACK reply %r" % reply)
            try:
                st2 = self.status()
            except ConnectionError_ as exc:
                raise ResetError("transaction", "STATUS after ROLLBACK failed: %s" % exc)
            if st2.get("TX") != "0":
                raise ResetError("transaction", "TX still open after ROLLBACK: %r" % st2)
            report.rolled_back = True
        report.steps.append("transaction")

    def _step_drain(self, report: ResetReport) -> None:
        # 丢弃客户端读缓冲 + 内核缓冲里残留的迟到响应。
        # 第一次探测为空立即返回（常见路径零额外延迟）；一旦发现残留
        # 数据，就在 grace 窗口内继续收迟到字节（例如超时后服务端
        # 慢半拍发出的响应）。
        drained = len(self._rbuf)
        self._rbuf = b""
        grace = self.drain_grace
        # 发生过读超时的连接，迟到响应此刻可能还在路上，也要等 grace。
        wait_for_stragglers = drained > 0 or self._timed_out
        try:
            saw_data = drained > 0
            while True:
                ready, _, _ = select.select([self.sock], [], [], 0)
                if ready:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise ResetError("drain", "peer closed while draining")
                    drained += len(chunk)
                    saw_data = True
                    continue
                if saw_data or wait_for_stragglers:
                    # 再等一个 grace 窗口确认没有迟到的尾巴
                    ready, _, _ = select.select([self.sock], [], [], grace)
                    if ready:
                        saw_data = True
                        continue
                break
        except ResetError:
            raise
        except OSError as exc:
            raise ResetError("drain", "recv failed: %s" % exc)
        self._timed_out = False
        report.drained_bytes = drained
        report.steps.append("drain")

    def _step_ping(self, report: ResetReport) -> None:
        # 端到端校验：此时缓冲已空、无事务，PING 必须恰好收到 PONG。
        # 若收到别的内容，说明命令-响应已错位，连接不可信。
        try:
            resp = self._cmd("PING")
        except ConnectionError_ as exc:
            raise ResetError("ping", "PING failed: %s" % exc)
        if resp != "PONG":
            raise ResetError("ping", "expected PONG, got %r (stream desynced)" % resp)
        report.steps.append("ping")

    # ------------------------------------------------------------ 关闭

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed
