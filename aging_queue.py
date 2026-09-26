"""Deterministic priority queue with discrete priority aging.

Priorities are non-negative integers. Smaller values have higher priority.
A task gains one priority level after every ``aging_period`` dequeues it waits
through; priority 0 is the highest effective priority.
"""

from dataclasses import dataclass
import heapq
from typing import Any, Optional


class Empty(Exception):
    """Raised when dequeue is called on an empty queue."""


@dataclass
class _Task:
    sequence: int
    item: Any
    static_priority: int
    effective_priority: int
    enqueue_tick: int
    active: bool = True
    next_promotion_due: Optional[int] = None


@dataclass
class DequeuedTask:
    """Information returned when a task is removed from the queue."""

    sequence: int
    item: Any
    priority: int
    effective_priority: int
    enqueue_tick: int
    dequeue_tick: int
    waited_dequeues: int


class AgingPriorityQueue:
    """A strict priority queue with FIFO tie breaking and configurable aging.

    Args:
        aging_period: Number of waited dequeues required to gain one priority
            level. ``None`` disables aging. The corresponding aging rate is
            ``1 / aging_period`` priority levels per dequeue.
    """

    def __init__(self, aging_period: Optional[int] = 8) -> None:
        if aging_period is not None:
            if not isinstance(aging_period, int) or aging_period <= 0:
                raise ValueError("aging_period must be a positive integer or None")
        self._aging_period = aging_period
        self._tick = 0
        self._next_sequence = 0
        self._size = 0
        self._buckets = {}
        self._nonempty_levels = []
        self._promotions = []

    def qsize(self) -> int:
        return self._size

    def empty(self) -> bool:
        return self._size == 0

    def enqueue(self, item: Any, priority: int = 0) -> int:
        """Add an item and return its globally ordered arrival sequence."""
        if not isinstance(priority, int) or priority < 0:
            raise ValueError("priority must be a non-negative integer")

        sequence = self._next_sequence
        self._next_sequence += 1
        task = _Task(
            sequence=sequence,
            item=item,
            static_priority=priority,
            effective_priority=priority,
            enqueue_tick=self._tick,
        )
        self._insert(task)
        if self._aging_period is not None and priority > 0:
            task.next_promotion_due = self._tick + self._aging_period
            heapq.heappush(
                self._promotions,
                (task.next_promotion_due, sequence, task),
            )
        self._size += 1
        return sequence

    def dequeue(self) -> DequeuedTask:
        """Remove and return the oldest task at the highest effective priority."""
        if self._size == 0:
            raise Empty("dequeue from an empty priority queue")

        level, task = self._peek()
        heapq.heappop(self._buckets[level])
        task.active = False

        dequeue_tick = self._tick + 1
        waited_dequeues = dequeue_tick - task.enqueue_tick
        result = DequeuedTask(
            sequence=task.sequence,
            item=task.item,
            priority=task.static_priority,
            effective_priority=task.effective_priority,
            enqueue_tick=task.enqueue_tick,
            dequeue_tick=dequeue_tick,
            waited_dequeues=waited_dequeues,
        )

        self._size -= 1
        self._tick = dequeue_tick
        self._apply_due_promotions()
        return result

    def _insert(self, task: _Task) -> None:
        level = task.effective_priority
        bucket = self._buckets.get(level)
        if bucket is None:
            bucket = []
            self._buckets[level] = bucket
        if not bucket:
            heapq.heappush(self._nonempty_levels, level)
        heapq.heappush(bucket, (task.sequence, task))

    def _peek(self) -> tuple:
        while self._nonempty_levels:
            level = self._nonempty_levels[0]
            bucket = self._buckets[level]
            while bucket:
                _, task = bucket[0]
                if task.active and task.effective_priority == level:
                    return level, task
                heapq.heappop(bucket)
            heapq.heappop(self._nonempty_levels)
        raise Empty("priority queue indexes are inconsistent")

    def _apply_due_promotions(self) -> None:
        if self._aging_period is None:
            return
        while self._promotions and self._promotions[0][0] <= self._tick:
            due, _, task = heapq.heappop(self._promotions)
            if not task.active or task.next_promotion_due != due:
                continue
            task.effective_priority -= 1
            self._insert(task)
            if task.effective_priority > 0:
                task.next_promotion_due = due + self._aging_period
                heapq.heappush(
                    self._promotions,
                    (task.next_promotion_due, task.sequence, task),
                )
            else:
                task.next_promotion_due = None
