"""时间轮：可注入时钟，休眠后限流触发，避免集中爆发打爆下游。

修复要点
--------
1. 时间可注入：构造时传入 ``clock``（返回毫秒时间戳的可调用对象），
   所有时间判断都来自它，测试可模拟机器休眠后的时间跳变。
2. 单批上限：``max_fires_per_poll`` 限制一次 ``poll`` 返回的任务数，
   到期任务先进入 ready 队列，按批次排空，而不是时间跳到哪就一次性全放出。
   传 ``None`` 退化为旧行为（无上限），仅用于复现问题。
3. 过期策略：不丢弃、不隐式合并，而是“标记”。每个触发结果带
   ``overdue_ms``（相对截止时间延迟了多少毫秒），由调用方按自身语义决定
   丢弃或补偿；调度任务不保证幂等，丢弃会直接丢业务，合并会改变语义，
   标记是对调用方最安全的默认策略。需要合并的调用方可按 ``key`` 自行去重。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, List, Optional


@dataclass(frozen=True)
class FiredTask:
    """一次触发的结果。``overdue_ms == 0`` 表示准时触发，大于 0 表示过期触发。"""

    key: str
    deadline_ms: int
    payload: Any
    overdue_ms: int


class TimerTask:
    __slots__ = ("key", "deadline_ms", "payload", "rounds", "cancelled")

    def __init__(self, key: str, deadline_ms: int, payload: Any, rounds: int) -> None:
        self.key = key
        self.deadline_ms = deadline_ms
        self.payload = payload
        self.rounds = rounds
        self.cancelled = False


class TimingWheel:
    """单级时间轮（槽内任务带轮次计数，支持任意远的未来任务）。

    参数
    ----
    tick_ms: 槽粒度（毫秒），一个 tick 推进一个槽。
    wheel_size: 槽数量，一圈覆盖 ``tick_ms * wheel_size`` 毫秒。
    max_fires_per_poll: 单次 ``poll`` 最多返回的触发数；``None`` 表示无上限
        （休眠前的旧行为，仅用于复现集中触发）。
    clock: 返回当前单调毫秒时间戳的可调用对象，默认 ``time.monotonic``。
    """

    def __init__(
        self,
        tick_ms: int = 100,
        wheel_size: int = 512,
        max_fires_per_poll: Optional[int] = 1000,
        clock: Optional[Callable[[], int]] = None,
    ) -> None:
        if tick_ms <= 0:
            raise ValueError("tick_ms must be positive")
        if wheel_size <= 0:
            raise ValueError("wheel_size must be positive")
        if max_fires_per_poll is not None and max_fires_per_poll <= 0:
            raise ValueError("max_fires_per_poll must be positive or None")
        self._tick_ms = tick_ms
        self._wheel_size = wheel_size
        self._max_fires = max_fires_per_poll
        self._clock = clock or (lambda: int(time.monotonic() * 1000))
        self._slots: List[List[TimerTask]] = [[] for _ in range(wheel_size)]
        self._ready: Deque[TimerTask] = deque()
        self._pending = 0
        start = self._clock()
        self._current_tick = start // tick_ms
        self._lock = threading.RLock()

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def schedule(self, key: str, delay_ms: int, payload: Any = None) -> TimerTask:
        if delay_ms < 0:
            raise ValueError("delay_ms must be non-negative")
        now_ms = self._clock()
        deadline_ms = now_ms + delay_ms
        task = TimerTask(key, deadline_ms, payload, rounds=0)
        with self._lock:
            self._insert(task, deadline_ms)
            self._pending += 1
        return task

    def cancel(self, task: TimerTask) -> bool:
        with self._lock:
            if task.cancelled:
                return False
            task.cancelled = True
            return True

    def poll(self, now_ms: Optional[int] = None) -> List[FiredTask]:
        """推进时间轮并返回本批到期任务，数量不超过 ``max_fires_per_poll``。

        机器休眠造成的大跨度时间跳变只会让任务进入 ready 队列；一次 poll
        只放出一批，连续调用 poll 即可把积压按批排空。
        """
        if now_ms is None:
            now_ms = self._clock()
        with self._lock:
            self._advance(now_ms)
            fired: List[FiredTask] = []
            limit = self._max_fires
            while self._ready and (limit is None or len(fired) < limit):
                task = self._ready.popleft()
                if task.cancelled:
                    self._pending -= 1
                    continue
                overdue = now_ms - task.deadline_ms
                fired.append(
                    FiredTask(
                        key=task.key,
                        deadline_ms=task.deadline_ms,
                        payload=task.payload,
                        overdue_ms=overdue if overdue > 0 else 0,
                    )
                )
                self._pending -= 1
            return fired

    def _insert(self, task: TimerTask, deadline_ms: int) -> None:
        target_tick = deadline_ms // self._tick_ms
        diff = target_tick - self._current_tick
        if diff <= 0:
            self._ready.append(task)
            return
        task.rounds = (diff - 1) // self._wheel_size
        slot = target_tick % self._wheel_size
        self._slots[slot].append(task)

    def _advance(self, now_ms: int) -> None:
        target_tick = now_ms // self._tick_ms
        if target_tick < self._current_tick:
            return
        while self._current_tick < target_tick:
            self._current_tick += 1
            idx = self._current_tick % self._wheel_size
            bucket = self._slots[idx]
            if not bucket:
                continue
            kept: List[TimerTask] = []
            for task in bucket:
                if task.rounds > 0:
                    task.rounds -= 1
                    kept.append(task)
                else:
                    self._ready.append(task)
            bucket = kept
            self._slots[idx] = bucket
