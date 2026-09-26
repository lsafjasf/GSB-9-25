"""最早截止优先（EDF）调度器实现。

排序键：
    (有截止时间标志, deadline, seq)
其中有截止时间的任务 flag=0，无截止时间任务 flag=1；deadline 相同按提交
顺序 seq；无截止时间任务内部同样按 seq（提交顺序）。

超期规则：
    出队/推进时间时，若 now + duration > deadline（可容忍等待时间 < 0），
    任务立即被标记为 OVERDUE、移出排队结构、产生且仅产生一条告警，之后
    不再参与排序、不会被重复出队、时间再推进也不会重复告警。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .model import Alert, Clock, Status, Task, tolerable_wait


@dataclass(frozen=True)
class DequeueResult:
    """一次 dequeue 的结果。overdue 为本次一并驱逐的超期任务。"""

    task: Optional[Task]
    now: float
    tolerable_wait: float
    overdue: Tuple[Task, ...]


class Scheduler:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._heap: List[Tuple[int, float, int, str]] = []
        # flag: 0=有截止时间, 1=无截止时间；键为 (flag, deadline, seq, id)
        self._tasks: Dict[str, Task] = {}
        self._status: Dict[str, str] = {}
        self._alerts: Dict[str, Alert] = {}
        self._seq = 0

    # ---- 提交 / 取消 / 完成 -------------------------------------------------

    def submit(
        self,
        task_id: str,
        duration: float,
        deadline: Optional[float] = None,
    ) -> Task:
        if task_id in self._tasks:
            raise ValueError(f"任务 id 重复: {task_id!r}")
        now = self._clock.now()
        task = Task(
            id=task_id,
            duration=float(duration),
            deadline=None if deadline is None else float(deadline),
            submitted_at=now,
            seq=self._seq,
        )
        self._seq += 1
        self._tasks[task_id] = task
        self._status[task_id] = Status.PENDING
        flag = 1 if deadline is None else 0
        sort_deadline = float("inf") if deadline is None else float(deadline)
        heapq.heappush(self._heap, (flag, sort_deadline, task.seq, task_id))
        return task

    def cancel(self, task_id: str) -> str:
        """取消任务，返回取消后的确定状态。

        - PENDING：标记 CANCELED 并惰性移出堆，永不再参与排序/告警。
        - OVERDUE：已是确定终态，返回 OVERDUE（告警已发生，不撤销、不补发）。
        - RUNNING / COMPLETED：非可取消状态，原样返回当前状态。
        """
        task = self._require_task(task_id)
        current = self._status[task_id]
        if current == Status.PENDING:
            self._status[task_id] = Status.CANCELED
        return self._status[task_id]

    def complete(self, task_id: str) -> None:
        """标记已出队任务执行完成。"""
        self._require_task(task_id)
        if self._status[task_id] != Status.RUNNING:
            raise ValueError(f"任务 {task_id!r} 不在运行中: {self._status[task_id]}")
        self._status[task_id] = Status.COMPLETED

    # ---- 出队 ---------------------------------------------------------------

    def tick(self) -> Tuple[Task, ...]:
        """推进检查：驱逐当前时刻已经无法按时完成的排队任务并告警。

        按 EDF 顺序从队首扫描；遇到仍可完成的任务即停止（它之前没有更早
        截止的任务，之后的任务不会比它更早超期）。一次时钟跳跃可跨越多条
        任务，它们都会在同一次 tick 中被驱逐。
        """
        now = self._clock.now()
        overdue: List[Task] = []
        heap = self._heap
        while heap:
            _, _, _, task_id = heap[0]
            status = self._status.get(task_id)
            if status != Status.PENDING:
                heapq.heappop(heap)  # 已取消等陈旧条目，惰性删除
                continue
            task = self._tasks[task_id]
            if task.deadline is None:
                break  # 无截止时间任务永不超期
            wait = tolerable_wait(now, task.deadline, task.duration)
            if wait >= 0:
                break  # 队首仍可完成，后面截止更晚/顺序更靠后
            heapq.heappop(heap)
            self._mark_overdue(task, now, wait)
            overdue.append(task)
        return tuple(overdue)

    def dequeue(self) -> Optional[DequeueResult]:
        """按最早截止优先出队一条可执行任务。

        出队前先驱逐超期任务；剩余时间不足以完成预计耗时的任务会被标记
        OVERDUE、触发一次告警，绝不继续排队等待。没有可执行任务时返回
        None（无截止时间任务只要队列里存在就仍可出队）。
        """
        now = self._clock.now()
        overdue = list(self.tick())
        heap = self._heap
        while heap:
            _, _, _, task_id = heapq.heappop(heap)
            if self._status.get(task_id) != Status.PENDING:
                continue  # 陈旧条目
            task = self._tasks[task_id]
            self._status[task_id] = Status.RUNNING
            return DequeueResult(
                task=task,
                now=now,
                tolerable_wait=tolerable_wait(now, task.deadline, task.duration),
                overdue=tuple(overdue),
            )
        if overdue:
            return DequeueResult(
                task=None,
                now=now,
                tolerable_wait=float("inf"),
                overdue=tuple(overdue),
            )
        return None

    # ---- 查询 ---------------------------------------------------------------

    def status_of(self, task_id: str) -> str:
        return self._status[task_id]

    def get_task(self, task_id: str) -> Task:
        return self._tasks[task_id]

    @property
    def alerts(self) -> Tuple[Alert, ...]:
        """按触发顺序返回告警（每个任务至多一条）。"""
        return tuple(self._alerts.values())

    def alert_count(self) -> int:
        return len(self._alerts)

    def pending_count(self) -> int:
        return sum(1 for s in self._status.values() if s == Status.PENDING)

    # ---- 内部 ---------------------------------------------------------------

    def _mark_overdue(self, task: Task, now: float, wait: float) -> None:
        # 状态守卫 + 字典守卫双重保证：只在跨越阈值的一刻告警一次。
        if self._status[task.id] != Status.PENDING or task.id in self._alerts:
            return
        self._status[task.id] = Status.OVERDUE
        self._alerts[task.id] = Alert(
            task_id=task.id,
            at=now,
            deadline=task.deadline,
            duration=task.duration,
            remaining=task.deadline - now,
            tolerable_wait=wait,
        )

    def _require_task(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise KeyError(f"未知任务: {task_id!r}") from None
