"""限流器库：令牌桶 + 滑动窗口，仅依赖标准库，时钟可注入。

统一接口：
    limiter.allow(n=1) -> Decision(allowed, wait)
        allowed: 是否放行
        wait:    建议等待秒数（放行时恒为 0.0）
    limiter.set_params(...)  参数变更立即生效，不重置时间状态
    limiter.stats() -> Stats(allowed, rejected, level, average_wait)

时钟异常策略（clock 为单调时钟语义，但实现做了防御）：
    - 回拨（now < 上次记录时间）：忽略该读数，视为"没有流逝任何时间"，
      且绝不把时间状态往前拨，因此回拨区间不会被重复计为可补充时间，
      配额不会被重复发放。
    - 前跳（now 远大于上次记录时间）：令牌桶补充量被容量上限截断，
      滑动窗口一次性淘汰全部过期事件，两者都不会因为长时间未调用
      而累积出超额配额，也不会卡死（建议等待时长始终有界）。

并发原子性：所有"检查 + 扣减"都在同一把锁内完成，判定与状态更新
对调用方表现为原子操作，任意并发度下不会超发配额。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional

ClockFn = Callable[[], float]


@dataclass(frozen=True)
class Decision:
    """单次放行判定结果。"""

    allowed: bool
    wait: float  # 建议等待秒数；放行时为 0.0


@dataclass(frozen=True)
class Stats:
    """可观测统计快照。

    level: 令牌桶为当前剩余令牌数（可为小数）；滑动窗口为窗口内已用配额数。
    average_wait: 所有 allow() 调用建议等待时长的平均值（放行的调用计 0）。
    """

    allowed: int
    rejected: int
    level: float
    average_wait: float


class _BaseLimiter:
    """公共骨架：时钟注入、统计、线程安全。"""

    def __init__(self, clock: ClockFn = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._allowed = 0
        self._rejected = 0
        self._wait_sum = 0.0
        self._calls = 0

    # -- 统计 ----------------------------------------------------------
    def _record(self, wait: float) -> Decision:
        allowed = wait <= 0.0
        if allowed:
            self._allowed += 1
        else:
            self._rejected += 1
        self._wait_sum += max(0.0, wait)
        self._calls += 1
        return Decision(allowed=allowed, wait=0.0 if allowed else wait)

    def _level_locked(self) -> float:
        raise NotImplementedError

    def stats(self) -> Stats:
        with self._lock:
            avg = self._wait_sum / self._calls if self._calls else 0.0
            return Stats(
                allowed=self._allowed,
                rejected=self._rejected,
                level=self._level_locked(),
                average_wait=avg,
            )


class TokenBucketLimiter(_BaseLimiter):
    """令牌桶：允许不超过容量的短时突发，长期速率不超过 refill_rate。"""

    def __init__(
        self,
        capacity: float,
        refill_rate: float,
        clock: ClockFn = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be positive")
        super().__init__(clock)
        self._capacity = float(capacity)
        self._rate = float(refill_rate)
        self._tokens = float(capacity)  # 初始满桶，允许开局突发
        self._last = self._clock()

    # -- 内部状态推进（须在锁内调用） -----------------------------------
    def _refill_locked(self, now: float) -> None:
        if now < self._last:
            # 时钟回拨：忽略该读数，时间状态不前移，回拨区间不计入补充。
            return
        elapsed = now - self._last
        self._last = now
        if elapsed > 0.0:
            # 前跳再大也被容量截断，不会累积超额配额。
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    def _level_locked(self) -> float:
        return self._tokens

    # -- 公共接口 -------------------------------------------------------
    def allow(self, n: float = 1.0) -> Decision:
        if n <= 0:
            raise ValueError("n must be positive")
        with self._lock:
            now = self._clock()
            self._refill_locked(now)
            if self._tokens >= n:
                self._tokens -= n
                return self._record(0.0)
            wait = (n - self._tokens) / self._rate
            return self._record(wait)

    def set_params(
        self,
        capacity: Optional[float] = None,
        refill_rate: Optional[float] = None,
    ) -> None:
        """变更容量/速率，立即生效；保留当前水位与时间状态。"""
        with self._lock:
            now = self._clock()
            self._refill_locked(now)  # 先按旧参数结算到当前时刻
            if capacity is not None:
                if capacity <= 0:
                    raise ValueError("capacity must be positive")
                self._capacity = float(capacity)
            if refill_rate is not None:
                if refill_rate <= 0:
                    raise ValueError("refill_rate must be positive")
                self._rate = float(refill_rate)
            # 水位超过新容量时截断；不重置 _last，时间状态连续。
            self._tokens = min(self._tokens, self._capacity)


class SlidingWindowLimiter(_BaseLimiter):
    """滑动窗口（事件日志式）：任意 window 秒区间内的放行数严格 <= limit。

    与令牌桶的差异：不允许"预支突发后立刻匀速补充"，每个事件占用的
    配额必须等满整个窗口才释放，因此窗口边界处没有突发放量。
    """

    def __init__(
        self,
        limit: int,
        window: float,
        clock: ClockFn = time.monotonic,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window <= 0:
            raise ValueError("window must be positive")
        super().__init__(clock)
        self._limit = int(limit)
        self._window = float(window)
        self._events: Deque[float] = deque()

    def _evict_locked(self, now: float) -> None:
        # 时钟回拨：now - window 变小，淘汰变保守，旧事件保留更久，
        # 不会因此腾出额外配额。前跳：一次性清空，立即恢复服务。
        cutoff = now - self._window
        events = self._events
        while events and events[0] <= cutoff:
            events.popleft()

    def _level_locked(self) -> float:
        return float(len(self._events))

    def allow(self, n: int = 1) -> Decision:
        if n <= 0:
            raise ValueError("n must be positive")
        with self._lock:
            now = self._clock()
            self._evict_locked(now)
            if len(self._events) + n <= self._limit:
                self._events.extend([now] * n)
                return self._record(0.0)
            # 需等最旧的事件滑出窗口才有空位；等待时长有界（<= window）。
            oldest = self._events[0]
            wait = max(0.0, oldest + self._window - now)
            return self._record(wait)

    def set_params(
        self,
        limit: Optional[int] = None,
        window: Optional[float] = None,
    ) -> None:
        """变更配额/窗口，立即生效；保留事件日志，按新窗口立即淘汰。"""
        with self._lock:
            if limit is not None:
                if limit <= 0:
                    raise ValueError("limit must be positive")
                self._limit = int(limit)
            if window is not None:
                if window <= 0:
                    raise ValueError("window must be positive")
                self._window = float(window)
            # 用新窗口立即结算一次；事件时间戳原样保留，不重置历史。
            self._evict_locked(self._clock())
