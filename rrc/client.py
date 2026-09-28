"""请求响应关联层。

- 每个请求分配单调递增唯一 id，响应按 id 配对。
- 未知 id / 重复响应 / 迟到（过期）响应分别计数并丢弃。
- 超时隔离：超时即完成并移除关联项，迟到响应只计 late，不改写任何状态。
- 并发上限：on_full="fail" 快速失败，on_full="queue" 排队等待。
- 显式取消：cancel(rid) 或取消协程，关联表与计时器立即回收。
- 断开/重连：on_disconnect="fail" 立即失败全部在途请求；
  on_disconnect="retry" 缓冲并在重连后重发（仍受原始超时约束，绝不悬挂）。
"""
from __future__ import annotations

import asyncio
import math
import sys
import time
from collections import Counter, OrderedDict
from typing import Any, Dict, Optional, Set, Tuple

from .channel import ChannelDown, SimulatedChannel


class BusyError(Exception):
    """on_full="fail" 且达到并发上限时抛出。"""


class ChannelError(Exception):
    """通道断开导致请求失败。"""


class RequestTimeout(TimeoutError):
    """请求超时。"""


class RequestFailed(Exception):
    """对端返回错误。"""


class _Entry:
    __slots__ = ("future", "timer", "payload")

    def __init__(self, future: asyncio.Future, timer: asyncio.TimerHandle, payload: Any) -> None:
        self.future = future
        self.timer = timer
        self.payload = payload


_STATS_BY_OUTCOME = {
    "ok": "succeeded",
    "timeout": "timed_out",
    "cancelled": "cancelled",
    "failed": "failed",
}


def _deep_sizeof(obj: Any, _seen: Optional[Set[int]] = None) -> int:
    """递归深层字节数；同一对象只计一次（共享的 outcome 常量字符串不重复计费）。"""
    if _seen is None:
        _seen = set()
    oid = id(obj)
    if oid in _seen:
        return 0
    _seen.add(oid)
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        for key, value in obj.items():
            size += _deep_sizeof(key, _seen) + _deep_sizeof(value, _seen)
    elif isinstance(obj, (tuple, list, set, frozenset)):
        for item in obj:
            size += _deep_sizeof(item, _seen)
    return size


class Client:
    """请求响应关联客户端。

    墓碑表（tombstone）记录最近完结请求的终态，用于把未配对的响应分类为：
    duplicate（重复）/ late（迟到）/ expired（墓碑已过期或被驱逐）/ unknown（未知 id）。
    容量 tombstone_size 与存活时间 tombstone_ttl 均可配置，内存占用有界：
    条目数 <= min(tombstone_size, 吞吐 x tombstone_ttl)，与请求总量无关。
    """

    def __init__(
        self,
        channel: SimulatedChannel,
        *,
        max_inflight: int = 128,
        on_full: str = "fail",
        on_disconnect: str = "fail",
        default_timeout: float = 5.0,
        tombstone_size: int = 4096,
        tombstone_ttl: Optional[float] = None,
    ) -> None:
        assert on_full in ("fail", "queue")
        assert on_disconnect in ("fail", "retry")
        assert tombstone_size >= 0
        assert tombstone_ttl is None or tombstone_ttl > 0
        self.channel = channel
        self.on_full = on_full
        self.on_disconnect = on_disconnect
        self.default_timeout = default_timeout
        self.tombstone_size = tombstone_size
        self.tombstone_ttl = tombstone_ttl

        self._pending: Dict[int, _Entry] = {}
        # 有界墓碑表：rid -> (终态, 过期时刻)。容量与 TTL 双重约束，懒惰驱逐。
        self._tombstones: "OrderedDict[int, Tuple[str, float]]" = OrderedDict()
        self._slots = asyncio.Semaphore(max_inflight)
        self._seq = 0
        self.stats: Counter = Counter()

        channel.on_message = self._on_message
        channel.on_disconnect = self._on_disconnect
        channel.on_connect = self._on_connect

    # ------------------------------------------------------------------ API

    async def call(self, payload: Any, timeout: Optional[float] = None) -> Any:
        """发起一次请求并等待配对响应。可能抛出：
        BusyError / RequestTimeout / ChannelError / RequestFailed / CancelledError。
        """
        timeout = self.default_timeout if timeout is None else timeout
        if self.on_full == "fail":
            if self._slots.locked():
                self.stats["rejected"] += 1
                raise BusyError("max inflight reached")
            await self._slots.acquire()
        else:
            await self._slots.acquire()
        try:
            return await self._call(payload, timeout)
        finally:
            self._slots.release()

    def cancel(self, rid: int) -> bool:
        """显式取消在途请求，立即回收关联表项与计时器。"""
        return self._drop(rid, "cancelled", asyncio.CancelledError(f"request {rid} cancelled"))

    @property
    def inflight(self) -> int:
        return len(self._pending)

    def check_consistent(self) -> None:
        """校验统计自洽：submitted == 成功+超时+失败+取消，且无悬挂状态。"""
        s = self.stats
        done = s["succeeded"] + s["timed_out"] + s["failed"] + s["cancelled"]
        assert done == s["submitted"], f"stats inconsistent: {done} != {s['submitted']}"
        assert not self._pending, f"{len(self._pending)} dangling entries"
        assert len(self._tombstones) <= self.tombstone_size

    def tombstone_memory(self) -> int:
        """实测墓碑表当前占用的深层字节数（共享字符串只计一次）。"""
        return _deep_sizeof(self._tombstones)

    @staticmethod
    def tombstone_entry_cost() -> int:
        """实测单条墓碑的深层字节数（当前解释器），用于内存上界推导。"""
        probe: "OrderedDict[int, Tuple[str, float]]" = OrderedDict()
        base = _deep_sizeof(probe)
        for i in range(1, 1001):
            probe[i] = ("timeout", float(i))
        return (_deep_sizeof(probe) - base) // 1000

    def tombstone_bound(self, rate: Optional[float] = None) -> int:
        """墓碑表内存上界（字节）推导值。

        条目数 <= min(tombstone_size, ceil(rate x tombstone_ttl))；
        rate 为吞吐（请求/秒），仅在设置 tombstone_ttl 时收紧上界。
        推导方法：构造同构探针表实测深层字节数，再乘 2 倍余量——
        CPython 字典删除不收缩，稳态下 entries 数组至多含等量 dummy 槽位。
        """
        entries = self.tombstone_size
        if self.tombstone_ttl is not None and rate is not None:
            entries = min(entries, math.ceil(rate * self.tombstone_ttl))
        probe: "OrderedDict[int, Tuple[str, float]]" = OrderedDict()
        for i in range(entries):
            probe[2**30 + i] = ("timeout", math.inf)  # 大 int 键，保守取形
        return 2 * _deep_sizeof(probe)

    # ------------------------------------------------------------- internal

    async def _call(self, payload: Any, timeout: float) -> Any:
        loop = asyncio.get_running_loop()
        self._seq += 1
        rid = self._seq
        fut: asyncio.Future = loop.create_future()
        timer = loop.call_later(timeout, self._expire, rid)
        self._pending[rid] = _Entry(fut, timer, payload)
        self.stats["submitted"] += 1
        if len(self._pending) > self.stats["peak_inflight"]:
            self.stats["peak_inflight"] = len(self._pending)
        try:
            self._send(rid, payload)
        except ChannelDown as exc:
            self._drop(rid, "failed", ChannelError(str(exc)))
        try:
            return await fut
        except asyncio.CancelledError:
            self._drop(rid, "cancelled", None)  # 调用方协程被取消，立即清理
            raise

    def _send(self, rid: int, payload: Any) -> None:
        try:
            self.channel.send({"id": rid, "payload": payload})
            self.stats["sent"] += 1
        except ChannelDown:
            if self.on_disconnect == "retry":
                self.stats["buffered"] += 1  # 留在 pending，重连后重发
            else:
                raise

    def _drop(self, rid: int, outcome: str, exc: Optional[BaseException]) -> bool:
        entry = self._pending.pop(rid, None)
        if entry is None:
            return False
        entry.timer.cancel()
        self.stats[_STATS_BY_OUTCOME[outcome]] += 1
        self._tombstone(rid, outcome)
        if exc is not None and not entry.future.done():
            entry.future.set_exception(exc)
        return True

    def _expire(self, rid: int) -> None:
        self._drop(rid, "timeout", RequestTimeout(f"request {rid} timed out"))

    def _tombstone(self, rid: int, outcome: str) -> None:
        now = time.monotonic()
        self._evict_expired(now)
        expires = now + self.tombstone_ttl if self.tombstone_ttl is not None else math.inf
        self._tombstones[rid] = (outcome, expires)
        self._tombstones.move_to_end(rid)
        while len(self._tombstones) > self.tombstone_size:
            self._tombstones.popitem(last=False)

    def _evict_expired(self, now: float) -> None:
        # TTL 恒定 => 过期时刻随插入序单调，从队首扫描即可
        while self._tombstones:
            _, expires = next(iter(self._tombstones.values()))
            if expires > now:
                break
            self._tombstones.popitem(last=False)

    # --------------------------------------------------------- channel hooks

    def _on_message(self, msg: dict) -> None:
        rid = msg.get("id")
        entry = self._pending.pop(rid, None)
        if entry is not None:
            entry.timer.cancel()
            if "error" in msg:
                self.stats["failed"] += 1
                self._tombstone(rid, "failed")
                if not entry.future.done():
                    entry.future.set_exception(RequestFailed(str(msg["error"])))
            else:
                self.stats["succeeded"] += 1
                self._tombstone(rid, "ok")
                if not entry.future.done():
                    entry.future.set_result(msg.get("payload"))
            return
        self._evict_expired(time.monotonic())
        tomb = self._tombstones.get(rid)
        outcome = tomb[0] if tomb is not None else None
        if outcome == "ok":
            self.stats["duplicate"] += 1  # 重复响应（重放）
        elif outcome is not None:
            self.stats["late"] += 1       # 迟到响应（超时/取消/失败之后）
            self.stats[f"late_{outcome}"] += 1  # 子原因：late_timeout/late_cancelled/late_failed
        elif isinstance(rid, int) and 0 < rid <= self._seq:
            self.stats["expired"] += 1    # 过期响应：墓碑已被 TTL/容量驱逐
        else:
            self.stats["unknown"] += 1    # 未知 id
        # 四种情况一律丢弃，不触碰任何现有状态

    def _on_disconnect(self) -> None:
        if self.on_disconnect == "fail":
            for rid in list(self._pending):
                self._drop(rid, "failed", ChannelError("channel disconnected"))
        # retry 策略：保留 pending，原始超时计时器继续走，结果有确定上界

    def _on_connect(self) -> None:
        if self.on_disconnect == "retry":
            for rid, entry in list(self._pending.items()):
                try:
                    self.channel.send({"id": rid, "payload": entry.payload})
                    self.stats["resent"] += 1
                except ChannelDown:
                    pass  # 仍未连上，等下一次重连或超时
