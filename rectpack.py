"""rectpack — 固定宽度条带矩形打包库（仅标准库）。

算法：MaxRects 空闲矩形集 + 可切换的排序/选择启发式。
给定容器宽度，在条带中放置矩形，最小化使用高度；支持容器高度上限与多容器。

可切换策略：
  - 放置顺序 SortOrder: MAXSIDE_DESC（长边降序，默认）/ AREA_DESC（面积降序）/
    HEIGHT_DESC（高度降序）/ INPUT（严格按输入顺序）。
  - 选择规则 SelectRule: BSSF（最佳短边贴合）/ BAF（最佳面积贴合）。

确定性：同一输入与同一策略组合多次运行结果完全一致
（排序键与平局裁决均为确定规则，不依赖哈希序或时钟）。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence, Tuple

__all__ = ["Placement", "PackResult", "MultiPackResult", "StripPacker",
           "SortOrder", "SelectRule", "pack", "pack_multi"]


class SortOrder(Enum):
    """放置顺序策略。"""
    MAXSIDE_DESC = "maxside_desc"   # 长边降序（历史默认行为）
    AREA_DESC = "area_desc"         # 面积降序
    HEIGHT_DESC = "height_desc"     # 高度降序
    INPUT = "input"                 # 严格按输入顺序


class SelectRule(Enum):
    """空位选择策略。"""
    BSSF = "bssf"   # 最佳短边贴合：短边剩余最小者优先
    BAF = "baf"     # 最佳面积贴合：空位面积剩余最小者优先


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
    """单容器打包结果。未放置的矩形通过 unplaced 明确列出，绝不悄悄丢弃。"""
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


@dataclass
class MultiPackResult:
    """多容器打包结果。每个容器一个 PackResult；放不下的矩形在 unplaced 中。"""
    container_width: float
    max_height: Optional[float]         # 每个容器的高度上限（None 表示不限）
    containers: List[PackResult] = field(default_factory=list)
    unplaced: List[int] = field(default_factory=list)   # 输入下标列表

    @property
    def used_area(self) -> float:
        return sum(c.used_area for c in self.containers)

    @property
    def height(self) -> float:
        """所有容器实际使用高度之和。"""
        return sum(c.height for c in self.containers)

    @property
    def utilization(self) -> float:
        """总利用率 = 已放置总面积 / (容器宽 * 各容器使用高度之和)。"""
        total = self.container_width * self.height
        if total <= 0:
            return 0.0
        return self.used_area / total


# 排序键：均含下标升序作为最终平局裁决，保证确定性。
_SORT_KEYS = {
    SortOrder.MAXSIDE_DESC: lambda t: (-max(t[1], t[2]), -(t[1] * t[2]), t[0]),
    SortOrder.AREA_DESC: lambda t: (-(t[1] * t[2]), -max(t[1], t[2]), t[0]),
    SortOrder.HEIGHT_DESC: lambda t: (-t[2], -(t[1] * t[2]), t[0]),
}


class _Bin:
    """单个容器的内部状态：空闲矩形集 + 已放置列表。"""

    __slots__ = ("width", "allow_rotate", "select", "free", "placements")

    def __init__(self, width: float, height_bound: float,
                 allow_rotate: bool, select: SelectRule):
        self.width = width
        self.allow_rotate = allow_rotate
        self.select = select
        self.free: List[tuple] = [(0.0, 0.0, width, float(height_bound))]
        self.placements: List[Placement] = []

    def try_place(self, idx: int, w: float, h: float) -> bool:
        node = self._find_best(w, h)
        if node is None:
            return False
        bx, by, pw, ph, rotated = node
        self.placements.append(Placement(idx, bx, by, pw, ph, rotated))
        self.free = self._split_and_prune(self.free, bx, by, pw, ph)
        return True

    def _orientations(self, w: float, h: float):
        yield (w, h, False)
        if self.allow_rotate and w != h:
            yield (h, w, True)

    def _find_best(self, w: float, h: float) -> Optional[tuple]:
        """按选择策略找最优空位；平局依次按补充键、y、x 裁决（确定性）。"""
        best = None
        best_key = None
        for fx, fy, fw, fh in self.free:
            for rw, rh, rot in self._orientations(w, h):
                if rw <= fw and rh <= fh:
                    short = min(fw - rw, fh - rh)   # 短边剩余
                    if self.select is SelectRule.BSSF:
                        key = (short, max(fw - rw, fh - rh), fy, fx)
                    else:  # BAF：空位面积剩余最小，短边剩余作平局裁决
                        key = (fw * fh - rw * rh, short, fy, fx)
                    if best_key is None or key < best_key:
                        best_key = key
                        best = (fx, fy, rw, rh, rot)
        return best

    @staticmethod
    def _split_and_prune(free, bx, by, bw, bh):
        """用已放置矩形切分所有相交的空闲矩形，并增量删除被包含者。

        只检查新生成矩形与现有矩形之间的包含关系（被包含的空闲矩形
        在任何"剩余越小越优"的规则下都不会胜出，删除不影响结果，只影响速度）。
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


class StripPacker:
    """固定宽度条带打包器。

    参数:
        width:        容器宽度，必须 > 0。
        allow_rotate: 是否允许矩形旋转 90 度。
        sort:         放置顺序策略（SortOrder），默认 MAXSIDE_DESC。
        select:       空位选择策略（SelectRule），默认 BSSF。
        max_height:   单容器高度上限；None 表示高度不限（默认）。
                      设置后放不下的矩形进入 unplaced（pack）
                      或触发开启新容器（pack_multi）。
        fixed_order:  已保留的兼容参数；True 等价于 sort=SortOrder.INPUT。
    """

    def __init__(self, width: float, allow_rotate: bool = False,
                 sort: SortOrder = SortOrder.MAXSIDE_DESC,
                 select: SelectRule = SelectRule.BSSF,
                 max_height: Optional[float] = None,
                 fixed_order: bool = False):
        if width <= 0:
            raise ValueError(f"容器宽度必须为正数，得到 {width!r}")
        if max_height is not None and max_height <= 0:
            raise ValueError(f"容器高度上限必须为正数，得到 {max_height!r}")
        self.width = float(width)
        self.allow_rotate = bool(allow_rotate)
        self.sort = SortOrder.INPUT if fixed_order else SortOrder(sort)
        self.select = SelectRule(select)
        self.max_height = None if max_height is None else float(max_height)

    # ---- 公共接口 ----

    def pack(self, rects: Sequence[Tuple[float, float]]) -> PackResult:
        """打包到单个容器，返回 PackResult。

        确定行为：
          - 空集合 -> 返回 height=0、无放置、无未放置的结果。
          - 任一维度 <= 0 -> 抛出 ValueError（零尺寸/负尺寸无版面意义）。
          - 放不进容器的矩形（过宽、或超出 max_height）-> 进入 unplaced。
        """
        items = self._prepare(rects)
        if not items:
            return PackResult(container_width=self.width, height=0.0)

        bin_ = _Bin(self.width, self._height_bound(items),
                    self.allow_rotate, self.select)
        unplaced = []
        for idx, w, h in items:
            if not bin_.try_place(idx, w, h):
                unplaced.append(idx)
        return self._make_result(bin_, unplaced)

    def pack_multi(self, rects: Sequence[Tuple[float, float]],
                   max_containers: Optional[int] = None) -> MultiPackResult:
        """打包到多个容器（通常配合构造参数 max_height 使用）。

        按确定顺序依次尝试已有容器，都放不下则开启新容器；
        达到 max_containers 上限或空容器也放不下时，矩形进入 unplaced。
        """
        if max_containers is not None and max_containers <= 0:
            raise ValueError(f"容器数量上限必须为正数，得到 {max_containers!r}")
        items = self._prepare(rects)
        if not items:
            return MultiPackResult(container_width=self.width,
                                   max_height=self.max_height)

        bound = self._height_bound(items)
        bins: List[_Bin] = []
        unplaced: List[int] = []
        for idx, w, h in items:
            if any(b.try_place(idx, w, h) for b in bins):
                continue
            if max_containers is not None and len(bins) >= max_containers:
                unplaced.append(idx)
                continue
            b = _Bin(self.width, bound, self.allow_rotate, self.select)
            if b.try_place(idx, w, h):
                bins.append(b)
            else:
                unplaced.append(idx)      # 空容器也放不下（如过宽/超高）

        containers = [self._make_result(b, []) for b in bins]
        unplaced.sort()
        return MultiPackResult(self.width, self.max_height, containers, unplaced)

    # ---- 内部实现 ----

    def _prepare(self, rects) -> List[tuple]:
        """校验尺寸、按策略排序，返回 (下标, 宽, 高) 列表。"""
        items = []
        for i, r in enumerate(rects):
            w, h = float(r[0]), float(r[1])
            if w <= 0 or h <= 0:
                raise ValueError(
                    f"矩形 #{i} 尺寸非法 ({r[0]!r} x {r[1]!r})：宽和高必须为正数")
            items.append((i, w, h))
        key = _SORT_KEYS.get(self.sort)
        if key is not None:
            items.sort(key=key)
        return items

    def _height_bound(self, items) -> float:
        if self.max_height is not None:
            return self.max_height
        # 高度上界：所有矩形长边之和，足以容纳任何合法摆放。
        return max(sum(max(w, h) for _, w, h in items), self.width)

    def _make_result(self, bin_: _Bin, unplaced: List[int]) -> PackResult:
        placements = sorted(bin_.placements, key=lambda p: p.index)
        unplaced = sorted(unplaced)
        height = max((p.y + p.height for p in placements), default=0.0)
        return PackResult(self.width, height, placements, unplaced)


def pack(width: float, rects: Sequence[Tuple[float, float]],
         allow_rotate: bool = False, fixed_order: bool = False,
         sort: SortOrder = SortOrder.MAXSIDE_DESC,
         select: SelectRule = SelectRule.BSSF,
         max_height: Optional[float] = None) -> PackResult:
    """便捷函数：等价于 StripPacker(width, ...).pack(rects)。"""
    return StripPacker(width, allow_rotate=allow_rotate, sort=sort,
                       select=select, max_height=max_height,
                       fixed_order=fixed_order).pack(rects)


def pack_multi(width: float, rects: Sequence[Tuple[float, float]],
               allow_rotate: bool = False,
               sort: SortOrder = SortOrder.MAXSIDE_DESC,
               select: SelectRule = SelectRule.BSSF,
               max_height: Optional[float] = None,
               max_containers: Optional[int] = None) -> MultiPackResult:
    """便捷函数：等价于 StripPacker(width, ...).pack_multi(rects, ...)。"""
    return StripPacker(width, allow_rotate=allow_rotate, sort=sort,
                       select=select,
                       max_height=max_height).pack_multi(rects, max_containers)
