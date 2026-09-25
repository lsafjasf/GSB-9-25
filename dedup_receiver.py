"""消息去重接收器（仅标准库）。

设计要点
--------
* 每个消息来源（source_id）维护一个 IPsec 抗重放式滑动窗口：
  已见最大序号 highest + 一个 W 比特位图（bit i 表示 highest-i 是否已收）。
* 窗口内重复            -> 丢弃，计 duplicate_dropped。
* 窗口内重复但内容不同  -> 丢弃，计 conflict_dropped（先到的生效，确定性行为）。
* 窗口外的迟到消息      -> 丢弃，计 expired_dropped（明确策略：丢弃并计数）。
* 标识缺失/非法         -> 丢弃，计 abnormal_id。
* 序号大幅跳跃          -> 窗口整体前移，跳跃之前的未收序号全部视为过期。
* 序号回绕              -> 可选 modulus 参数（如 2**32），按模距离判断新旧。
* 内存有界              -> 来源状态用 LRU 淘汰，上限 max_sources；
                           每个来源状态 = W 比特位图 + W*2 字节内容指纹，与消息总量无关。

不变式（统计自洽）：
    received == accepted + duplicate_dropped + expired_dropped
              + conflict_dropped + abnormal_id
"""

from __future__ import annotations

import zlib
from array import array
from collections import OrderedDict
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Callable, Optional


class Verdict(Enum):
    ACCEPTED = "accepted"          # 首次到达，交付处理
    DUPLICATE = "duplicate"        # 窗口内重复，丢弃
    EXPIRED = "expired"            # 窗口外迟到，丢弃
    CONFLICT = "conflict"          # 标识重复但内容不同，丢弃（先到生效）
    ABNORMAL = "abnormal"          # 标识缺失/非法，丢弃


@dataclass
class Stats:
    received: int = 0            # 进入 receive() 的消息总数
    accepted: int = 0            # 判定为首次到达并交付处理
    duplicate_dropped: int = 0   # 窗口内重复
    expired_dropped: int = 0     # 窗口外迟到
    conflict_dropped: int = 0    # 标识重复但内容指纹不同
    abnormal_id: int = 0         # 标识缺失或非法
    sources_evicted: int = 0     # 因 LRU 上限被淘汰的来源数（非逐条计数，不参与不变式）

    def check_invariant(self) -> bool:
        return self.received == (
            self.accepted
            + self.duplicate_dropped
            + self.expired_dropped
            + self.conflict_dropped
            + self.abnormal_id
        )

    def as_dict(self) -> dict:
        return asdict(self)


class _SourceWindow:
    """单个来源的去重窗口。状态大小只取决于 window_size，与消息量无关。"""

    __slots__ = ("highest", "bitmap", "fingerprints")

    def __init__(self, window_size: int) -> None:
        self.highest = -1                 # 尚未收到任何消息
        self.bitmap = 0                   # bit i: highest-i 是否已收
        self.fingerprints = array("H", bytes(2 * window_size))  # 环形槽: seq % W -> 16bit 内容指纹


class DedupReceiver:
    """基于 (source_id, seq) 的消息去重接收器。

    参数:
        window_size: 每个来源的去重窗口 W（比特数）。乱序容忍度 = W-1 个序号。
        max_sources: 同时跟踪的来源上限，超出按 LRU 淘汰（内存有界的关键）。
        modulus:     序号空间模数（如 2**32），None 表示序号单调不回绕。
                     要求 window_size <= modulus // 2。
        on_message:  消息被判定为首次到达时的回调 on_message(source_id, seq, payload)，
                     每条唯一消息恰好调用一次（exactly-once 处理钩子）。
    """

    def __init__(
        self,
        window_size: int = 1024,
        max_sources: int = 4096,
        modulus: Optional[int] = None,
        on_message: Optional[Callable[[str, int, bytes], None]] = None,
    ) -> None:
        if window_size < 8:
            raise ValueError("window_size 至少为 8")
        if max_sources < 1:
            raise ValueError("max_sources 至少为 1")
        if modulus is not None and window_size > modulus // 2:
            raise ValueError("window_size 必须 <= modulus // 2，否则回绕时新旧无法区分")
        self.window_size = window_size
        self.max_sources = max_sources
        self.modulus = modulus
        self.on_message = on_message
        self.stats = Stats()
        self._mask = (1 << window_size) - 1
        self._sources: "OrderedDict[str, _SourceWindow]" = OrderedDict()

    # ------------------------------------------------------------------ API

    def receive(self, source_id, seq, payload: bytes = b"") -> Verdict:
        """接收一条消息，返回判定结果；仅 ACCEPTED 时触发 on_message。"""
        self.stats.received += 1

        if not self._valid(source_id, seq):
            self.stats.abnormal_id += 1
            return Verdict.ABNORMAL

        if isinstance(payload, str):
            payload = payload.encode("utf-8", "replace")
        fingerprint = zlib.crc32(payload) & 0xFFFF

        window = self._sources.get(source_id)
        if window is None:
            window = self._open_source(source_id)
        else:
            self._sources.move_to_end(source_id)

        verdict = self._classify(window, seq, fingerprint)

        if verdict is Verdict.ACCEPTED:
            self.stats.accepted += 1
            if self.on_message is not None:
                self.on_message(source_id, seq, payload)
        elif verdict is Verdict.DUPLICATE:
            self.stats.duplicate_dropped += 1
        elif verdict is Verdict.EXPIRED:
            self.stats.expired_dropped += 1
        else:  # CONFLICT
            self.stats.conflict_dropped += 1
        return verdict

    @property
    def tracked_sources(self) -> int:
        return len(self._sources)

    # ------------------------------------------------------------- internal

    def _valid(self, source_id, seq) -> bool:
        if source_id is None or source_id == "":
            return False
        if isinstance(seq, bool) or not isinstance(seq, int):
            return False
        if seq < 0:
            return False
        if self.modulus is not None and seq >= self.modulus:
            return False
        return True

    def _open_source(self, source_id: str) -> _SourceWindow:
        if len(self._sources) >= self.max_sources:
            self._sources.popitem(last=False)  # 淘汰最久未用的来源
            self.stats.sources_evicted += 1
        window = _SourceWindow(self.window_size)
        self._sources[source_id] = window
        return window

    def _classify(self, window: _SourceWindow, seq: int, fingerprint: int) -> Verdict:
        W = self.window_size
        if window.highest < 0:
            # 该来源的第一条消息：直接接受
            window.highest = seq
            window.bitmap = 1
            window.fingerprints[seq % W] = fingerprint
            return Verdict.ACCEPTED

        modulus = self.modulus
        if modulus is not None:
            forward = (seq - window.highest) % modulus
            is_new = 0 < forward <= modulus // 2
            offset = 0 if is_new else (window.highest - seq) % modulus
            shift = forward if is_new else 0
        else:
            if seq > window.highest:
                is_new, shift, offset = True, seq - window.highest, 0
            else:
                is_new, shift, offset = False, 0, window.highest - seq

        if is_new:
            # 序号前进（含大幅跳跃）：窗口整体前移 shift 位
            if shift >= W:
                window.bitmap = 1
            else:
                window.bitmap = ((window.bitmap << shift) | 1) & self._mask
            window.highest = seq
            window.fingerprints[seq % W] = fingerprint
            return Verdict.ACCEPTED

        if offset >= W:
            return Verdict.EXPIRED  # 窗口外迟到：丢弃并计数

        slot = seq % W
        if (window.bitmap >> offset) & 1:
            # 窗口内重复：比对内容指纹区分“真重复”与“同标识不同内容”
            return Verdict.DUPLICATE if window.fingerprints[slot] == fingerprint else Verdict.CONFLICT

        # 窗口内的乱序新消息：补位接受
        window.bitmap |= 1 << offset
        window.fingerprints[slot] = fingerprint
        return Verdict.ACCEPTED
