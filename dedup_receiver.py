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

自适应窗口（auto_adapt=True 时启用，默认关闭，行为与旧版完全一致）
------------------------------------------------------------------
* 持续观测每个来源的“乱序深度”（迟到消息到达时，已有多少个更新序号先到），
  用有界、可合并的分位数直方图（ReorderHistogram）给出指定分位数（默认 p99）
  与目标误判率的窗口建议值：suggest_window(source_id) / suggest_cluster_window()。
* 观测样本足够后按迟滞规则调整该来源窗口（增长 >=1.25x 或收缩 <=0.5x 才动作）。
* **安全性：窗口调整永不改变任何已确定消息的判定结果。**
  - 收缩仅截断当前为空（未收到）的高位 bit，已记录消息全部保留；
  - 增长时维护 trusted_floor：旧窗口外的历史区域一律继续按 EXPIRED 处理，
    绝不把窗口变大之前已过期的消息“复活”成 ACCEPTED（否则破坏 exactly-once）。
    新扩大窗口在收满 W_new 个连续前进序号后自然完成“信任刷新”。

不变式（统计自洽）：
    received == accepted + duplicate_dropped + expired_dropped
              + conflict_dropped + abnormal_id
"""

from __future__ import annotations

import math
import zlib
from array import array
from collections import OrderedDict
from dataclasses import dataclass, asdict, field
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
    window_resized: int = 0      # 自适应窗口调整次数（增长+收缩，非逐条计数，不参与不变式）

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


class ReorderHistogram:
    """乱序深度的有界分位数直方图（定长计数数组，内存固定，可跨实例合并）。

    桶方案（共 72 桶，深度单位为“序号个数”）：
      * 0..15      -> 16 个线性桶，每个深度一桶（精确）；
      * 段 k=0..6  -> 覆盖 [16*2^k, 32*2^k)，每段 8 个桶、桶宽 2^(k+1)，
                      依次覆盖到 [1024, 2048)；
      * 2048 及以上 -> 尾桶，分位数按 2048 保守取值（再由 max_window_size 截断）。
    粗桶一律返回桶上界，建议窗口时宁可偏大也不漏判。
    """

    LINEAR = 16
    BUCKETS = 72
    TAIL_DEPTH = 2048

    def __init__(self) -> None:
        self.counts = array("Q", bytes(8 * self.BUCKETS))  # unsigned long long
        self.total = 0

    @classmethod
    def _index_of(cls, depth: int) -> int:
        if depth < 0:
            depth = 0
        if depth < cls.LINEAR:
            return depth
        if depth >= cls.TAIL_DEPTH:
            return cls.BUCKETS - 1
        # depth in [16, 2048)：段 k 覆盖 [16*2^k, 32*2^k)，桶宽 2^(k+1)
        k = min(6, (depth // cls.LINEAR).bit_length() - 1)
        base = cls.LINEAR + 8 * k
        width = 2 << k
        return min(cls.BUCKETS - 1, base + (depth - (cls.LINEAR << k)) // width)

    @classmethod
    def _upper_edge(cls, idx: int) -> int:
        """桶的保守上界（建议窗口时用上界，宁可偏大不漏判）。"""
        if idx < cls.LINEAR:
            return idx
        if idx == cls.BUCKETS - 1:
            return cls.TAIL_DEPTH
        j = idx - cls.LINEAR
        k = j // 8
        width = 2 << k
        within = j % 8
        return (cls.LINEAR << k) + (within + 1) * width - 1

    def add(self, depth: int) -> None:
        self.counts[self._index_of(depth)] += 1
        self.total += 1

    def merge(self, other: "ReorderHistogram") -> None:
        for i in range(self.BUCKETS):
            self.counts[i] += other.counts[i]
        self.total += other.total

    def quantile(self, q: float = 0.99) -> Optional[int]:
        """返回乱序深度的 q 分位数（nearest-rank，粗桶取上界）。样本不足返回 None。"""
        if not 0.0 < q <= 1.0:
            raise ValueError("q 必须在 (0, 1]")
        if self.total == 0:
            return None
        rank = max(1, math.ceil(q * self.total))
        cumulative = 0
        for i in range(self.BUCKETS):
            cumulative += self.counts[i]
            if cumulative >= rank:
                return self._upper_edge(i)
        return self._upper_edge(self.BUCKETS - 1)

    def suggest_window(
        self,
        quantile: float = 0.99,
        max_window_size: int = 4096,
        min_window_size: int = 8,
    ) -> Optional[int]:
        """基于观测分位数的窗口建议值：W = 分位深度 + 1（容忍度 = W-1）。

        返回 None 表示样本不足、暂无建议。
        """
        depth_q = self.quantile(quantile)
        if depth_q is None:
            return None
        suggested = depth_q + 1
        return max(min_window_size, min(max_window_size, suggested))

    def as_counts(self) -> list:
        return list(self.counts)


class _SourceWindow:
    """单个来源的去重窗口。状态大小只取决于当前 window_size，与消息量无关。"""

    __slots__ = ("highest", "bitmap", "fingerprints", "trusted_floor",
                 "hist", "obs_since_adapt")

    def __init__(self, window_size: int) -> None:
        self.highest = -1                       # 尚未收到任何消息
        self.bitmap = 0                        # bit i: highest-i 是否已收（仅在 >= trusted_floor 时可信）
        self.fingerprints = array("H", bytes(2 * window_size))  # 环形槽: seq % W -> 16bit 内容指纹
        # 窗口增长安全性：序号 < trusted_floor 的历史区域“未被当前大窗口实际观察过”，
        # 即使 bitmap 补位也一律按 EXPIRED 处理（绝不复活增长前已过期的消息）。
        # None 表示窗口从未增长、整张位图均可信（固定窗口语义）。
        self.trusted_floor = None
        self.hist = ReorderHistogram()
        self.obs_since_adapt = 0

    def all_bits_trusted(self, modulus: Optional[int], window_size: int) -> bool:
        """当前窗口覆盖的最旧序号是否已进入可信区（即增长后的窗口已被新消息完全刷新）。"""
        if self.trusted_floor is None:
            return True
        oldest = self.highest - (window_size - 1)
        if modulus is None:
            return oldest >= self.trusted_floor
        return (self.highest - self.trusted_floor) % modulus >= window_size - 1


class DedupReceiver:
    """基于 (source_id, seq) 的消息去重接收器，支持乱序分位数驱动的自适应窗口。

    参数:
        window_size: 每个来源的初始窗口 W（比特数），乱序容忍度 = W-1 个序号。
        max_sources: 同时跟踪的来源上限，超出按 LRU 淘汰（内存有界的关键）。
        modulus:     序号空间模数（如 2**32），None 表示序号单调不回绕。
                     要求窗口大小 <= modulus // 2。
        on_message:  消息被判定为首次到达时的回调 on_message(source_id, seq, payload)，
                     每条唯一消息恰好调用一次（exactly-once 处理钩子）。
        auto_adapt:  是否按观测乱序分位数自动调整各来源窗口。默认 False，
                     关闭时行为与固定窗口版本完全一致。
        quantile:    自适应/建议所用的乱序分位数，默认 0.99。
        min_window_size / max_window_size: 自适应窗口的上下限。
        adapt_min_samples: 每个来源累计多少乱序观测后才允许第一次调整。
        adapt_every: 两次调整之间至少新增多少观测（迟滞，避免抖动）。
    """

    def __init__(
        self,
        window_size: int = 1024,
        max_sources: int = 4096,
        modulus: Optional[int] = None,
        on_message: Optional[Callable[[str, int, bytes], None]] = None,
        *,
        auto_adapt: bool = False,
        quantile: float = 0.99,
        min_window_size: Optional[int] = None,
        max_window_size: Optional[int] = None,
        adapt_min_samples: int = 128,
        adapt_every: int = 64,
    ) -> None:
        if window_size < 8:
            raise ValueError("window_size 至少为 8")
        if max_sources < 1:
            raise ValueError("max_sources 至少为 1")
        min_window_size = window_size if min_window_size is None else min_window_size
        max_window_size = max(window_size, 4096) if max_window_size is None else max_window_size
        if not 8 <= min_window_size <= window_size:
            raise ValueError("需满足 8 <= min_window_size <= window_size(初始)")
        if max_window_size < window_size:
            raise ValueError("max_window_size 不能小于初始 window_size")
        if modulus is not None and max_window_size > modulus // 2:
            raise ValueError("窗口大小必须 <= modulus // 2，否则回绕时新旧无法区分")
        self.window_size = window_size
        self.max_sources = max_sources
        self.modulus = modulus
        self.on_message = on_message
        self.auto_adapt = auto_adapt
        self.quantile = quantile
        self.min_window_size = min_window_size
        self.max_window_size = max_window_size
        self.adapt_min_samples = adapt_min_samples
        self.adapt_every = adapt_every
        self.stats = Stats()
        self._mask = (1 << window_size) - 1
        self._sources: "OrderedDict[str, _SourceWindow]" = OrderedDict()
        self.global_hist = ReorderHistogram()  # 全实例（全部分片可合并）的乱序观测

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

        if self.auto_adapt:
            self._maybe_adapt(source_id, window)
        return verdict

    @property
    def tracked_sources(self) -> int:
        return len(self._sources)

    def window_size_of(self, source_id: str) -> Optional[int]:
        """某来源当前窗口大小；来源尚未建立/已被 LRU 淘汰时返回 None。"""
        w = self._sources.get(source_id)
        return None if w is None else len(w.fingerprints)

    def suggest_window(self, source_id: str) -> Optional[int]:
        """基于该来源自身乱序观测的窗口建议值（W = 分位深度 + 1，含上下限截断）。"""
        w = self._sources.get(source_id)
        if w is None:
            return None
        return w.hist.suggest_window(self.quantile, self.max_window_size,
                                     self.min_window_size)

    def observed_quantile(self, source_id: str) -> Optional[int]:
        w = self._sources.get(source_id)
        return None if w is None else w.hist.quantile(self.quantile)

    def suggest_cluster_window(self) -> Optional[int]:
        """基于本实例全部来源合并观测的（全局）窗口建议值，可用于统一配置/跨分片汇总。"""
        return self.global_hist.suggest_window(self.quantile, self.max_window_size,
                                               self.min_window_size)

    def reorder_histogram(self, source_id: str) -> Optional[ReorderHistogram]:
        w = self._sources.get(source_id)
        return None if w is None else w.hist

    def resize_window_for(self, source_id: str, new_size: int) -> Optional[int]:
        """显式调整某来源窗口（建议值落地），返回调整后的实际大小；来源不存在返回 None。

        安全性：不改变任何已确定消息的判定（见模块说明中的 trusted_floor 机制）。
        新大小会被截断到 [min_window_size, max_window_size]。
        """
        window = self._sources.get(source_id)
        if window is None:
            return None
        self._sources.move_to_end(source_id)
        new_size = max(self.min_window_size, min(self.max_window_size, int(new_size)))
        if window.highest < 0:
            window.fingerprints = array("H", bytes(2 * new_size))
            return new_size
        applied = self._resize(window, new_size)
        if applied is not None:
            window.obs_since_adapt = 0
        return len(window.fingerprints)

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

    def _is_trusted(self, window: _SourceWindow, seq: int, offset: int) -> bool:
        """序号 seq（距 highest 的向后距离 offset）是否落在已建立信任的窗口区域。"""
        if window.trusted_floor is None:
            return True
        if self.modulus is None:
            return seq >= window.trusted_floor
        return (window.highest - window.trusted_floor) % self.modulus >= offset

    def _record_reorder(self, window: _SourceWindow, depth: int) -> None:
        if depth <= 0:
            return
        window.hist.add(depth)
        window.obs_since_adapt += 1
        self.global_hist.add(depth)

    def _classify(self, window: _SourceWindow, seq: int, fingerprint: int) -> Verdict:
        W = len(window.fingerprints)
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
                window.bitmap = ((window.bitmap << shift) | 1) & ((1 << W) - 1)
            window.highest = seq
            window.fingerprints[seq % W] = fingerprint
            return Verdict.ACCEPTED

        if offset >= W:
            # 窗口外迟到：记录真实乱序深度（即使是重复也先观测，避免幸存者偏差），
            # 再按过期策略丢弃。
            self._record_reorder(window, offset)
            return Verdict.EXPIRED

        if not self._is_trusted(window, seq, offset):
            # 窗口增长后尚未被新数据覆盖的历史区域：延续增长前的判定 -> 一律 EXPIRED。
            # 这是“调整不改变已确定消息判定”的关键：增长不复活任何旧消息。
            self._record_reorder(window, offset)
            return Verdict.EXPIRED

        slot = seq % W
        if (window.bitmap >> offset) & 1:
            # 窗口内重复：比对内容指纹区分“真重复”与“同标识不同内容”
            return Verdict.DUPLICATE if window.fingerprints[slot] == fingerprint else Verdict.CONFLICT

        # 窗口内的乱序新消息：补位接受，并记录一次乱序深度观测
        window.bitmap |= 1 << offset
        window.fingerprints[slot] = fingerprint
        self._record_reorder(window, offset)
        return Verdict.ACCEPTED

    def _maybe_adapt(self, source_id: str, window: _SourceWindow) -> None:
        if window.hist.total < self.adapt_min_samples:
            return
        if window.obs_since_adapt < self.adapt_every:
            return
        target = window.hist.suggest_window(self.quantile, self.max_window_size,
                                            self.min_window_size)
        if target is None:
            return
        current = len(window.fingerprints)
        # 迟滞：相对偏离足够大才动作（增长需 >=1.25x，收缩需 <=0.5x）
        grow = 4 * target >= 5 * current and target > current
        shrink = target <= current // 2 and target < current
        if not (grow or shrink):
            window.obs_since_adapt = 0  # 观测已消费，避免每条消息重复计算
            return
        self._resize(window, target)
        window.obs_since_adapt = 0
