"""限流器：令牌桶 + 滑动窗口，统一接口。

统一接口
--------
``try_acquire(n=1) -> Decision(allowed, wait_seconds)``
    - allowed: 是否放行
    - wait_seconds: 若被拒绝，建议等待多少秒后再试（放行时恒为 0）

并发原子性
----------
每个限流器实例持有一把互斥锁，"读时钟 -> 更新状态 -> 判定 -> 记录统计"
整个临界区都在锁内完成，因此任意并发度下配额不会被超发。

时钟异常策略
------------
- 回拨（now < 上次观测时间）：检测到后计入 stats.clock_backwards。
  令牌桶把结算基准线重置到回拨后的时刻（不补发、不倒扣；由于补充量
  始终被 capacity 封顶，回拨再恢复也不会超发，且不会因此卡死）；
  滑动窗口不回溯驱逐，"未来"的记录仍占配额，不会重复发放。
- 向前跳跃：令牌桶补充量被 capacity 封顶，跳跃再大也只恢复到满桶，
  不会超发；滑动窗口按 now - window 驱逐过期记录，跳跃后窗口立即
  清空、立即可用，不会长时间卡死。

参数变更
--------
set_capacity / set_rate / set_window / set_max_requests 只改配置，
保留全部时间状态（last_time、剩余令牌、窗口时间戳队列），因此
立即生效且不丢失历史。容量调小时会把多余令牌截断到新容量。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, NamedTuple

from .clock import Clock, SystemClock
from .stats import Stats, StatsSnapshot


class Decision(NamedTuple):
    allowed: bool
    wait_seconds: float


class TokenBucketRateLimiter:
    """令牌桶：允许不超过 capacity 的短时突发，长期速率收敛到 rate/秒。

    行为特征：空闲时积攒令牌，突发可一次性打满 capacity；
    被拒时的等待时长是确定性的 (n - tokens) / rate。
    """

    def __init__(
        self,
        capacity: float,
        rate_per_second: float,
        clock: Clock | None = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if rate_per_second < 0:
            raise ValueError("rate_per_second must be >= 0")
        self._clock: Clock = clock or SystemClock()
        self._capacity = float(capacity)
        self._rate = float(rate_per_second)
        self._tokens = float(capacity)
        self._last = self._clock.now()
        self._last_seen = self._last
        self._stats = Stats()
        self._lock = threading.Lock()

    # ---- 统一接口 -------------------------------------------------------

    def try_acquire(self, n: int = 1) -> Decision:
        if n <= 0:
            raise ValueError("n must be > 0")
        with self._lock:
            now = self._now_locked()
            self._refill_locked(now)
            if self._tokens >= n:
                self._tokens -= n
                decision = Decision(True, 0.0)
            else:
                wait = self._wait_for_locked(n)
                decision = Decision(False, wait)
            self._stats.record(decision.allowed, decision.wait_seconds)
            return decision

    def stats(self) -> StatsSnapshot:
        with self._lock:
            now = self._now_locked()
            self._refill_locked(now)
            return self._stats.snapshot(level=self._tokens)

    # ---- 参数变更（保留时间状态，立即生效） ----------------------------

    def set_capacity(self, capacity: float) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        with self._lock:
            now = self._now_locked()
            self._refill_locked(now)
            self._capacity = float(capacity)
            self._tokens = min(self._tokens, self._capacity)

    def set_rate(self, rate_per_second: float) -> None:
        if rate_per_second < 0:
            raise ValueError("rate_per_second must be >= 0")
        with self._lock:
            now = self._now_locked()
            self._refill_locked(now)  # 先按旧速率结算到 now，再换速率
            self._rate = float(rate_per_second)

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def rate_per_second(self) -> float:
        return self._rate

    # ---- 内部（须在锁内调用） ------------------------------------------

    def _now_locked(self) -> float:
        now = self._clock.now()
        if now < self._last_seen:
            self._stats.clock_backwards += 1
        self._last_seen = now
        return now

    def _refill_locked(self, now: float) -> None:
        if now < self._last:
            self._last = now  # 回拨：基准线重置到新时刻，不补发也不卡死
            return
        elapsed = now - self._last
        if elapsed > 0.0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now

    def _wait_for_locked(self, n: int) -> float:
        if self._rate <= 0.0:
            return float("inf")
        return max(0.0, (n - self._tokens) / self._rate)


class SlidingWindowRateLimiter:
    """滑动窗口：任意 window_seconds 内放行总数 <= max_requests。

    行为特征：无突发积攒概念，配额按"窗口内实际发生数"精确计数；
    被拒时的等待时长 = 窗口内最早一条记录滑出窗口的剩余时间。
    与令牌桶对比：令牌桶空闲后可一次性突发 capacity 个，滑动窗口
    在任何长度为 window 的区间内都严格不超 max_requests。
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        clock: Clock | None = None,
    ) -> None:
        if max_requests <= 0:
            raise ValueError("max_requests must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._clock: Clock = clock or SystemClock()
        self._max = int(max_requests)
        self._window = float(window_seconds)
        self._hits: Deque[float] = deque()
        self._last_seen = self._clock.now()
        self._stats = Stats()
        self._lock = threading.Lock()

    # ---- 统一接口 -------------------------------------------------------

    def try_acquire(self, n: int = 1) -> Decision:
        if n <= 0:
            raise ValueError("n must be > 0")
        with self._lock:
            now = self._now_locked()
            self._evict_locked(now)
            if len(self._hits) + n <= self._max:
                self._hits.extend([now] * n)
                decision = Decision(True, 0.0)
            else:
                decision = Decision(False, self._wait_for_locked(n, now))
            self._stats.record(decision.allowed, decision.wait_seconds)
            return decision

    def stats(self) -> StatsSnapshot:
        with self._lock:
            now = self._now_locked()
            self._evict_locked(now)
            return self._stats.snapshot(level=float(len(self._hits)))

    # ---- 参数变更（保留时间戳队列，立即生效） ---------------------------

    def set_max_requests(self, max_requests: int) -> None:
        if max_requests <= 0:
            raise ValueError("max_requests must be > 0")
        with self._lock:
            now = self._now_locked()
            self._evict_locked(now)
            self._max = int(max_requests)

    def set_window(self, window_seconds: float) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        with self._lock:
            now = self._now_locked()
            self._window = float(window_seconds)
            self._evict_locked(now)  # 按新窗口立即重新裁剪旧记录

    @property
    def max_requests(self) -> int:
        return self._max

    @property
    def window_seconds(self) -> float:
        return self._window

    # ---- 内部（须在锁内调用） ------------------------------------------

    def _now_locked(self) -> float:
        now = self._clock.now()
        if now < self._last_seen:
            self._stats.clock_backwards += 1
        self._last_seen = now
        return now

    def _evict_locked(self, now: float) -> None:
        cutoff = now - self._window
        # 浮点边界容差：长时间累计的时间戳存在 ~1e-13 级舍入误差，
        # 没有容差会让刚好到期的记录多滞留一个判定周期。
        eps = 1e-9 * max(1.0, abs(now), self._window)
        while self._hits and self._hits[0] <= cutoff + eps:
            self._hits.popleft()

    def _wait_for_locked(self, n: int, now: float) -> float:
        # 需要等窗口内第 (len - (max - n)) 条记录滑出窗口
        need_to_expire = len(self._hits) - (self._max - n)
        if need_to_expire <= 0:
            return 0.0
        oldest_blocking = self._hits[need_to_expire - 1]
        return max(0.0, oldest_blocking + self._window - now)
