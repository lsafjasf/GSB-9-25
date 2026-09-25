"""Earliest-deadline-first (EDF) task scheduler. Python 3, stdlib only.

Time is injected by the caller: `clock` is a zero-arg callable returning the
current time as a number. Tests drive it with a fake clock.

Semantics
---------
- Tasks may declare a `deadline` and must declare `estimated_duration`.
- Dequeue order: earliest deadline first; ties broken by submission order.
  Tasks without a deadline sort after all deadline tasks, FIFO among themselves.
- At dequeue time the scheduler computes the tolerable wait (slack =
  deadline - now). If slack < estimated_duration the task is marked overdue,
  exactly one alert is fired, and the task is dropped instead of being
  returned for execution (it must not keep waiting in the queue).
- An alert fires at most once per task, only when the task crosses the
  overdue threshold. Repeated dequeues or clock advances never re-fire it.
- Cancelled tasks never participate in ordering and never trigger alerts.
  Cancelling an already-overdue task returns the deterministic result
  CANCELLED_OVERDUE.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

CANCELLED = "cancelled"
CANCELLED_OVERDUE = "cancelled_overdue"
NOT_FOUND = "not_found"


@dataclass
class Task:
    task_id: str
    estimated_duration: float
    deadline: Optional[float] = None
    submit_seq: int = field(default=-1, init=False)
    overdue: bool = field(default=False, init=False)
    alerted: bool = field(default=False, init=False)
    cancelled: bool = field(default=False, init=False)


class Scheduler:
    def __init__(
        self,
        clock: Callable[[], float],
        on_alert: Optional[Callable[[Task], None]] = None,
    ) -> None:
        self._clock = clock
        self._on_alert = on_alert
        self._heap: list = []  # (deadline, submit_seq, task) for deadline tasks
        self._no_deadline: deque = deque()  # FIFO for tasks without deadline
        self._tasks: Dict[str, Task] = {}
        self._seq = 0
        self.alert_count = 0

    def submit(self, task: Task) -> Task:
        if task.task_id in self._tasks:
            raise ValueError(f"duplicate task id: {task.task_id}")
        task.submit_seq = self._seq
        self._seq += 1
        self._tasks[task.task_id] = task
        if task.deadline is None:
            self._no_deadline.append(task)
        else:
            heapq.heappush(self._heap, (task.deadline, task.submit_seq, task))
        return task

    def cancel(self, task_id: str) -> str:
        """Remove a task. Returns CANCELLED, CANCELLED_OVERDUE or NOT_FOUND."""
        task = self._tasks.pop(task_id, None)
        if task is None:
            return NOT_FOUND
        task.cancelled = True
        # Lazy removal: heap/FIFO entries are skipped when encountered.
        return CANCELLED_OVERDUE if task.overdue else CANCELLED

    def _fire_alert_once(self, task: Task) -> None:
        task.overdue = True
        if task.alerted:
            return
        task.alerted = True
        self.alert_count += 1
        if self._on_alert is not None:
            self._on_alert(task)

    def _is_overdue(self, task: Task) -> bool:
        slack = task.deadline - self._clock()
        return slack < task.estimated_duration

    def _pop_deadline_task(self) -> Optional[Task]:
        """Pop the head deadline task, discarding cancelled/overdue ones."""
        while self._heap:
            _, _, task = self._heap[0]
            heapq.heappop(self._heap)
            if task.cancelled:
                continue
            self._tasks.pop(task.task_id, None)
            if self._is_overdue(task):
                self._fire_alert_once(task)
                continue  # overdue tasks are dropped, not dispatched
            return task
        return None

    def dequeue(self) -> Optional[Task]:
        """Return the next runnable task, or None if the queue is empty.

        Overdue deadline tasks encountered at the head of the queue are
        marked, alerted exactly once, and dropped; the scan continues until
        a runnable task is found.
        """
        task = self._pop_deadline_task()
        if task is not None:
            return task
        while self._no_deadline:
            task = self._no_deadline.popleft()
            if task.cancelled:
                continue
            self._tasks.pop(task.task_id, None)
            return task
        return None

    def __len__(self) -> int:
        return len(self._tasks)
