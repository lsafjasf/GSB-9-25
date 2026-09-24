"""内置模拟通道：可注入延迟、乱序、丢包与重放，支持断开/重连。

仅用于测试与演示，替代真实传输层。乱序由随机延迟自然产生。
"""
from __future__ import annotations

import asyncio
import random
from typing import Callable, Optional, Tuple


class ChannelDown(Exception):
    """通道断开时调用 send 抛出。"""


class SimulatedChannel:
    def __init__(
        self,
        *,
        latency: Tuple[float, float] = (0.0, 0.005),
        loss: float = 0.0,
        replay: float = 0.0,
        slow: float = 0.0,
        slow_latency: Tuple[float, float] = (1.5, 2.0),
        seed: Optional[int] = None,
    ) -> None:
        self.latency = latency
        self.slow = slow
        self.slow_latency = slow_latency
        self.loss = loss
        self.replay = replay
        self.connected = True
        self.on_message: Optional[Callable[[dict], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.on_connect: Optional[Callable[[], None]] = None
        self._rng = random.Random(seed)
        # 通道侧统计（用于交叉校验）
        self.sent_count = 0
        self.delivered_count = 0
        self.dropped_count = 0

    def send(self, msg: dict) -> None:
        """模拟对端 echo 服务：收到请求后异步回送同 id 的响应。"""
        if not self.connected:
            raise ChannelDown("channel is down")
        self.sent_count += 1
        if self._rng.random() < self.loss:
            self.dropped_count += 1
            return
        resp = {"id": msg["id"], "payload": msg["payload"]}
        self._schedule(resp)
        if self._rng.random() < self.replay:
            self._schedule(resp)  # 重放：同一份响应再投递一次

    def inject(self, msg: dict) -> None:
        """测试用：直接向接收方注入一条消息（如未知 id）。"""
        if self.on_message:
            self.on_message(msg)

    def disconnect(self) -> None:
        if self.connected:
            self.connected = False
            if self.on_disconnect:
                self.on_disconnect()

    def reconnect(self) -> None:
        if not self.connected:
            self.connected = True
            if self.on_connect:
                self.on_connect()

    def _schedule(self, resp: dict) -> None:
        lo_hi = self.slow_latency if self._rng.random() < self.slow else self.latency
        delay = self._rng.uniform(*lo_hi)
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._deliver, resp)

    def _deliver(self, resp: dict) -> None:
        if not self.connected:
            self.dropped_count += 1  # 断开期间在途的报文丢失
            return
        self.delivered_count += 1
        if self.on_message:
            self.on_message(resp)
