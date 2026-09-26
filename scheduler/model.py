"""调度器的数据模型与时间抽象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

Time = float


class Status:
    """任务生命周期状态（字符串常量，便于断言与打印）。"""

    PENDING = "pending"      # 在队列中等待调度
    RUNNING = "running"      # 已出队，等待调用方执行/完成
    COMPLETED = "completed"
    OVERDUE = "overdue"      # 剩余时间不足以完成预计耗时，已超期
    CANCELED = "canceled"


@dataclass(frozen=True)
class Task:
    """一个可调度任务。

    deadline 为 None 表示无截止时间，排序时永远排在有截止时间的任务之后。
    """

    id: str
    duration: float
    deadline: Optional[float] = None
    submitted_at: Optional[float] = None
    seq: int = field(default=0, compare=False)

    def __post_init__(self) -> None:
        if self.duration < 0:
            raise ValueError("duration 必须非负")
        if (
            self.deadline is not None
            and self.submitted_at is not None
            and self.deadline < self.submitted_at
        ):
            raise ValueError("deadline 不得早于 submitted_at")


@dataclass(frozen=True)
class Alert:
    """超期告警：每个任务至多产生一条。"""

    task_id: str
    at: float
    deadline: Optional[float]
    duration: float
    remaining: float
    tolerable_wait: float


class Clock:
    """时间源接口。now() 必须单调不减。"""

    def now(self) -> float:  # pragma: no cover - 接口定义
        raise NotImplementedError


class FakeClock(Clock):
    """测试用时钟：时间只能由测试显式推进/跳跃。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, delta: float) -> float:
        if delta < 0:
            raise ValueError("时间不允许倒流")
        self._now += float(delta)
        return self._now

    def jump_to(self, value: float) -> float:
        if value < self._now:
            raise ValueError("时间不允许倒流")
        self._now = float(value)
        return self._now


def tolerable_wait(now: float, deadline: Optional[float], duration: float) -> float:
    """出队时刻可容忍的等待时间 = deadline - now - duration。

    - 无截止时间：inf（永远不会因时间而超期）。
    - 结果为负：即便立即执行也无法按时完成，任务已超期。
    """
    if deadline is None:
        return float("inf")
    return deadline - now - duration
