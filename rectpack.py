"""rectpack — 固定宽度条带矩形打包库（仅标准库）。

算法：MaxRects + Best Short Side Fit (BSSF) 启发式。
给定容器宽度，在高度不限的条带中放置矩形，最小化使用高度。

确定性：同一输入多次运行结果完全一致（排序与平局裁决均为确定规则）。
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

__all__ = ["Placement", "PackResult", "StripPacker", "pack"]


@dataclass(frozen=True)
class Placement:
    """一个已放置矩形的输出记录。"""
    index: int          # 输入 rects 中的下标
    x: float
    y: float
    width: float        # 放置后的宽（旋转后与输入可能互换）
    height: float       # 放置后的高
    rotated: bool = False


@dataclass
class PackResult:
    """打包结果。未放置的矩形通过 unplaced 明确列出，绝不悄悄丢弃。"""
    container_width: float
    height: float                       # 实际所需高度
    placements: List[Placement] = field(default_factory=list)
    unplaced: List[int] = field(default_factory=list)   # 输入下标列表

    @property
    def used_area(self) -> float:
        return sum(p.width * p.height for p in self.placements)

    @property
    def utilization(self) -> float:
        """面积利用率 = 已放置面积 / (容器宽 * 所需高度)。空集定义为 0.0。"""
        if self.height <= 0:
            return 0.0
        return self.used_area / (self.container_width * self.height)


class StripPacker:
    """固定宽度条带打包器。

    参数:
        width:        容器宽度，必须 > 0。
        allow_rotate: 是否允许矩形旋转 90 度。
        fixed_order:  True  -> 严格按输入顺序放置；
                      False -> 按确定规则重排（长边降序、面积降序、下标升序），
                               通常得到更紧凑的结果。
    """

    def __init__(self, width: float, allow_rotate: bool = False,
                 fixed_order: bool = False):
        if width <= 0:
            raise ValueError(f"容器宽度必须为正数，得到 {width!r}")
        self.width = float(width)
        self.allow_rotate = bool(allow_rotate)
        self.fixed_order = bool(fixed_order)

    def pack(self, rects: Sequence[Tuple[float, float]]) -> PackResult:
        """打包 rects（(宽, 高) 序列），返回 PackResult。

        确定行为：
          - 空集合 -> 返回 height=0、无放置、无未放置的结果。
          - 任一维度 <= 0 -> 抛出 ValueError（零尺寸/负尺寸无版面意义）。
          - 单个矩形放不进容器（过宽，且旋转也不可行）-> 进入 unplaced。
        """
        items = []
        for i, r in enumerate(rects):
            w, h = float(r[0]), float(r[1])
            if w <= 0 or h <= 0:
                raise ValueError(
                    f"矩形 #{i} 尺寸非法 ({r[0]!r} x {r[1]!r})：宽和高必须为正数")
            items.append((i, w, h))

        if not items:
            return PackResult(container_width=self.width, height=0.0)

        if not self.fixed_order:
            items.sort(key=lambda t: (-max(t[1], t[2]), -(t[1] * t[2]), t[0]))

        # 高度上界：所有矩形长边之和，足以容纳任何合法摆放。
        bound = sum(max(w, h) for _, w, h in items)
        bound = max(bound, self.width)
        free: List[tuple] = [(0.0, 0.0, self.width, float(bound))]

        placements: List[Placement] = []
        unplaced: List[int] = []

        for idx, w, h in items:
            node = self._find_best(free, w, h)
            if node is None:
                unplaced.append(idx)
                continue
            bx, by, pw, ph, rotated = node
            placements.append(Placement(idx, bx, by, pw, ph, rotated))
            free = self._split_and_prune(free, bx, by, pw, ph)

        placements.sort(key=lambda p: p.index)
        unplaced.sort()
        height = max((p.y + p.height for p in placements), default=0.0)
        return PackResult(self.width, height, placements, unplaced)

    # ---- 内部实现 ----

    def _orientations(self, w: float, h: float):
        yield (w, h, False)
        if self.allow_rotate and w != h:
            yield (h, w, True)

    def _find_best(self, free, w, h) -> Optional[tuple]:
        """BSSF：短边剩余最小者优先；平局依次按长边剩余、y、x 裁决（确定性）。"""
        best = None
        best_key = None
        for fx, fy, fw, fh in free:
            for rw, rh, rot in self._orientations(w, h):
                if rw <= fw and rh <= fh:
                    key = (min(fw - rw, fh - rh),   # 短边剩余
                           max(fw - rw, fh - rh),   # 长边剩余
                           fy, fx)                  # 位置平局裁决
                    if best_key is None or key < best_key:
                        best_key = key
                        best = (fx, fy, rw, rh, rot)
        return best

    @staticmethod
    def _split_and_prune(free, bx, by, bw, bh):
        """用已放置矩形切分所有相交的空闲矩形，并增量删除被包含者。

        只检查新生成矩形与现有矩形之间的包含关系（被包含的空闲矩形
        在 BSSF 下永远不会胜出，删除不影响结果，只影响速度）。
        """
        kept = []
        pending = []
        for f in free:
            fx, fy, fw, fh = f
            if (bx >= fx + fw or bx + bw <= fx or
                    by >= fy + fh or by + bh <= fy):
                kept.append(f)                    # 不相交，保留
                continue
            # 相交：最多切出 4 个新空闲矩形
            if bx > fx:
                pending.append((fx, fy, bx - fx, fh))
            if bx + bw < fx + fw:
                pending.append((bx + bw, fy, fx + fw - (bx + bw), fh))
            if by > fy:
                pending.append((fx, fy, fw, by - fy))
            if by + bh < fy + fh:
                pending.append((fx, by + bh, fw, fy + fh - (by + bh)))

        for p in pending:
            px, py, pw, ph = p
            pr, pb = px + pw, py + ph
            absorbed = False
            for f in kept:
                if (f[0] <= px and f[1] <= py and
                        f[0] + f[2] >= pr and f[1] + f[3] >= pb):
                    absorbed = True               # p 被现有矩形包含，丢弃
                    break
            if absorbed:
                continue
            kept = [f for f in kept
                    if not (px <= f[0] and py <= f[1] and
                            pr >= f[0] + f[2] and pb >= f[1] + f[3])]
            kept.append(p)
        return kept

def pack(width: float, rects: Sequence[Tuple[float, float]],
         allow_rotate: bool = False, fixed_order: bool = False) -> PackResult:
    """便捷函数：等价于 StripPacker(width, ...).pack(rects)。"""
    return StripPacker(width, allow_rotate=allow_rotate,
                       fixed_order=fixed_order).pack(rects)
