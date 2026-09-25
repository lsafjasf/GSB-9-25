"""可注入的时间来源。

所有限流器都只依赖 ``Clock`` 协议（一个 ``now() -> float`` 方法，
返回单调递增的秒数）。生产环境用 ``SystemClock``，测试用 ``FakeClock``
精确控制时间，包括回拨与跳跃。
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    """时间来源协议：返回当前时间（秒，float）。"""

    def now(self) -> float: ...


class SystemClock:
    """生产时钟：基于 time.monotonic()，不受系统校时影响。"""

    def now(self) -> float:
        return time.monotonic()


class FakeClock:
    """测试时钟：完全由调用方控制，可任意前进、回拨、跳跃。

    线程安全，可在并发测试中冻结时间（不 advance 时所有线程
    读到同一时刻），从而给出精确的配额断言。
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> float:
        """前进（seconds 为负即回拨），返回新的当前时间。"""
        with self._lock:
            self._t += seconds
            return self._t

    def set(self, t: float) -> None:
        with self._lock:
            self._t = float(t)
