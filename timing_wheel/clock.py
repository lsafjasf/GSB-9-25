"""可注入的手动时钟。

生产环境把 time.monotonic 传给时间轮即可；测试里用 ManualClock 精确控制
时间前进。内部用整数毫秒记账，避免浮点秒反复累加产生漂移。
sleep() 模拟「机器休眠」：时间一次性跳过去，中间没有任何 tick。
"""

from __future__ import annotations


class ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._ms: int = round(start * 1000)

    def __call__(self) -> float:
        return self._ms / 1000

    def sleep(self, seconds: float) -> None:
        """模拟休眠：时间瞬间前进 seconds 秒。"""
        self._ms += round(seconds * 1000)

    def tick(self, seconds: float) -> None:
        """普通流逝，语义同 sleep()，语义上表示「机器清醒时的时间前进」。"""
        self.sleep(seconds)

    def rewind(self, seconds: float) -> None:
        """时钟回拨（模拟 NTP 跳变）。"""
        self._ms -= round(seconds * 1000)
