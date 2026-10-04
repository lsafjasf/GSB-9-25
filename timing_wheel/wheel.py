"""修复后的时间轮：时钟跳变 / 机器休眠后，过期任务按批限速触发。

修复策略（过期任务处理）：**合并（coalesce）+ ＋ 限速消化**。

- 检测到时间跳变（一次 advance 跨过多个 tick）时，不重放每个错过的 tick，
  而是把所有过期任务按 (到期 tick, 提交顺序) 合并进一个积压队列；
- 每次 advance() 最多触发 max_fires_per_advance 个任务，且只有当时钟
  实际前进时才消化积压 —— 因此同一时刻的触发数量有硬上限；
- 选择「合并」而非「丢弃」：定时任务（重试、心跳、超时关闭等）通常仍然
  需要执行，直接丢弃会丢业务；「标记」则把补偿逻辑推给调用方。合并保留
  全部工作，只用速率保护下游。
- 对于「睡得太久、补跑已无意义」的场景，提供可选的 late_policy="drop"
  ＋ max_late_ms：过期超过该阈值的任务被丢弃并通过 on_event("drop", ...)
  显式标记出来，默认不启用。
"""

from __future__ import annotations

import itertools
import math
import time
from collections import deque
from dataclasses import dataclass, field

MERGE = "merge"
DROP = "drop"


@dataclass(frozen=True)
class Timer:
    deadline_tick: int
    seq: int
    payload: object


@dataclass
class Batch:
    """一次 advance() 的触发结果，即「分批数据」。"""

    index: int
    fired: list
    remaining_backlog: int
    skipped_ticks: int = 0
    dropped: list = field(default_factory=list)

    @property
    def fired_count(self) -> int:
        return len(self.fired)


class TimingWheel:
    def __init__(
        self,
        tick_ms: int = 100,
        wheel_size: int = 512,
        now=None,
        max_fires_per_advance: int = 100,
        late_policy: str = MERGE,
        max_late_ms: int | None = None,
        on_event=None,
    ) -> None:
        if tick_ms <= 0 or wheel_size <= 0 or max_fires_per_advance <= 0:
            raise ValueError("tick_ms / wheel_size / max_fires_per_advance 必须为正")
        if late_policy not in (MERGE, DROP):
            raise ValueError("late_policy 只能是 'merge' 或 'drop'")
        if late_policy == DROP and max_late_ms is None:
            raise ValueError("late_policy='drop' 需要同时给出 max_late_ms")
        self.tick_ms = tick_ms
        self.wheel_size = wheel_size
        self.now = now or time.monotonic
        self.max_fires_per_advance = max_fires_per_advance
        self.late_policy = late_policy
        self.max_late_ticks = None if max_late_ms is None else math.ceil(max_late_ms / tick_ms)
        self.on_event = on_event
        self._slots: list[list[Timer]] = [[] for _ in range(wheel_size)]
        self._backlog: deque[Timer] = deque()
        self._seq = itertools.count()
        self._batch_index = 0
        self.current_tick = self._tick_of(self.now())

    # ---- 基础 ----

    def _tick_of(self, ts: float) -> int:
        return round(ts * 1000) // self.tick_ms

    def add(self, delay_ms: int, payload) -> Timer:
        deadline = self.current_tick + max(1, math.ceil(delay_ms / self.tick_ms))
        timer = Timer(deadline, next(self._seq), payload)
        self._slots[deadline % self.wheel_size].append(timer)
        return timer

    @property
    def backlog_size(self) -> int:
        return len(self._backlog)

    @property
    def pending(self) -> int:
        return sum(len(s) for s in self._slots) + len(self._backlog)

    # ---- 推进 ----

    def _collect_due(self, target_tick: int) -> list[Timer]:
        """把 (current_tick, target_tick] 内到期的任务从槽位中取出，按到期时间排序。"""
        skipped = target_tick - self.current_tick
        if skipped >= self.wheel_size:
            slot_indexes = range(self.wheel_size)
        else:
            slot_indexes = ((self.current_tick + i) % self.wheel_size for i in range(1, skipped + 1))
        due: list[Timer] = []
        for idx in slot_indexes:
            slot = self._slots[idx]
            keep = [t for t in slot if t.deadline_tick > target_tick]
            if len(keep) != len(slot):
                due.extend(t for t in slot if t.deadline_tick <= target_tick)
                self._slots[idx] = keep
        due.sort(key=lambda t: (t.deadline_tick, t.seq))
        return due

    def advance(self) -> Batch:
        """推进到当前时间并触发一批任务。

        不变式：单次调用最多触发 max_fires_per_advance 个任务；
        时钟没有前进时不消化任何积压（防止调用方空转时再次打爆下游）。
        """
        target_tick = self._tick_of(self.now())
        if target_tick < self.current_tick:
            target_tick = self.current_tick  # 时钟回拨：不后退，当无事发生
        skipped = target_tick - self.current_tick

        dropped: list[Timer] = []
        if skipped > 0:
            due = self._collect_due(target_tick)
            if skipped > 1 and self.on_event:
                self.on_event("clock_jump", {"skipped_ticks": skipped - 1, "due": len(due)})
            if self.late_policy == DROP:
                cutoff = target_tick - self.max_late_ticks
                fresh = [t for t in due if t.deadline_tick >= cutoff]
                dropped = [t for t in due if t.deadline_tick < cutoff]
                due = fresh
                if dropped and self.on_event:
                    self.on_event("drop", [t.payload for t in dropped])
            self._backlog.extend(due)
            self.current_tick = target_tick

        fired: list = []
        if skipped > 0:
            while self._backlog and len(fired) < self.max_fires_per_advance:
                fired.append(self._backlog.popleft().payload)

        self._batch_index += 1
        return Batch(
            index=self._batch_index,
            fired=fired,
            remaining_backlog=len(self._backlog),
            skipped_ticks=max(0, skipped - 1),
            dropped=[t.payload for t in dropped],
        )
