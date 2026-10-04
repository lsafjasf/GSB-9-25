"""修复前的时间轮实现 —— 仅用于复现「休眠后集中触发」问题。

问题所在：advance() 会逐 tick 追平当前时间，把休眠期间积压的所有到期
任务在【同一次调用】里全部返回。机器休眠 1 小时、积压 5000 个任务时，
下游会在一瞬间收到 5000 次触发。
"""

from __future__ import annotations

import itertools
import math
import time


class BuggyTimingWheel:
    def __init__(self, tick_ms: int = 100, wheel_size: int = 512, now=None) -> None:
        self.tick_ms = tick_ms
        self.wheel_size = wheel_size
        self.now = now or time.monotonic
        self._slots: list[list] = [[] for _ in range(wheel_size)]
        self._seq = itertools.count()
        self.current_tick = self._tick_of(self.now())

    def _tick_of(self, ts: float) -> int:
        return round(ts * 1000) // self.tick_ms

    def add(self, delay_ms: int, payload) -> None:
        deadline = self.current_tick + max(1, math.ceil(delay_ms / self.tick_ms))
        self._slots[deadline % self.wheel_size].append((deadline, next(self._seq), payload))

    def advance(self) -> list:
        """推进到当前时间，返回本次触发的任务 —— 不设任何上限。"""
        target = self._tick_of(self.now())
        fired = []
        while self.current_tick <= target:
            idx = self.current_tick % self.wheel_size
            slot = self._slots[idx]
            keep = []
            for deadline, seq, payload in slot:
                if deadline <= self.current_tick:
                    fired.append(payload)
                else:
                    keep.append((deadline, seq, payload))
            self._slots[idx] = keep
            self.current_tick += 1
        return fired
