"""限流器可观测统计。"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class StatsSnapshot:
    """某一时刻的统计快照（不可变，可安全跨线程读取）。"""

    allowed: int            # 累计放行数
    rejected: int           # 累计拒绝数
    level: float            # 当前水位：令牌桶=剩余令牌数；滑动窗口=窗口内已用配额数
    avg_wait_seconds: float # 平均建议等待时长（对所有判定求平均，放行计 0）
    clock_backwards: int    # 检测到的时钟回拨次数


class Stats:
    """统计计数器。字段只在持有对应限流器锁时更新，读取快照亦在锁内完成。"""

    def __init__(self) -> None:
        self.allowed = 0
        self.rejected = 0
        self.wait_sum = 0.0
        self.decisions = 0
        self.clock_backwards = 0

    def record(self, allowed: bool, wait: float) -> None:
        if allowed:
            self.allowed += 1
        else:
            self.rejected += 1
        self.wait_sum += wait
        self.decisions += 1

    def snapshot(self, level: float) -> StatsSnapshot:
        avg = self.wait_sum / self.decisions if self.decisions else 0.0
        return StatsSnapshot(
            allowed=self.allowed,
            rejected=self.rejected,
            level=level,
            avg_wait_seconds=avg,
            clock_backwards=self.clock_backwards,
        )
