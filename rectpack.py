"""rectpack —— 矩形打包布局库（仅依赖 Python 标准库）。

给定固定宽度、高度不限的容器，把一批矩形面板尽量紧凑地放入，
输出每个矩形的坐标，并明确列出放不下的矩形。

核心算法：MaxRects（Best Short Side Fit, BSSF）
  - 维护一组"空闲矩形"（free rectangles）；
  - 每个矩形选择"短边剩余最小"的空闲矩形放置（平局时比较剩余面积、y、x，保证确定性）；
  - 放置后对所有与占位相交的空闲矩形做最大切分，并增量剪除被包含的空闲矩形。

朴素基线：按面积降序的 Shelf（层架）算法，用于利用率对比。

复杂度（n 个矩形，F 为空闲矩形数量，剪枝后通常为几十到几百）：
  - 时间：O(n * F)，最坏 O(n^2)；
  - 空间：O(F)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple, Union

__all__ = [
    "Rect",
    "PlacedRect",
    "UnplacedRect",
    "PackingResult",
    "pack",
    "pack_naive",
]


# ---------------------------------------------------------------- 数据模型

@dataclass(frozen=True)
class Rect:
    """待打包矩形。rid 为空时由 pack() 自动编号（r0, r1, ...）。"""
    w: float
    h: float
    rid: str = ""


@dataclass(frozen=True)
class PlacedRect:
    """已放置矩形：左上角坐标 (x, y)，w/h 为放置后的实际朝向尺寸。"""
    rid: str
    x: float
    y: float
    w: float
    h: float
    rotated: bool


@dataclass(frozen=True)
class UnplacedRect:
    """未能放置的矩形及其原因。"""
    rid: str
    w: float
    h: float
    reason: str


@dataclass
class PackingResult:
    """打包结果。height 为实际所需高度（所有已放置矩形的最大底边）。"""
    width: float
    height: float
    placed: List[PlacedRect]
    unplaced: List[UnplacedRect]

    @property
    def utilization(self) -> float:
        """面积利用率 = 已放置面积 / (容器宽 * 所需高度)。空容器返回 0.0。"""
        used = sum(p.w * p.h for p in self.placed)
        container = self.width * self.height
        return used / container if container > 0 else 0.0


# ---------------------------------------------------------------- 内部结构

class _FreeRect:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self.x, self.y, self.w, self.h = x, y, w, h


def _contains(outer: _FreeRect, inner: _FreeRect) -> bool:
    """outer 是否完整包含 inner（含边界相等）。"""
    return (outer.x <= inner.x and outer.y <= inner.y
            and outer.x + outer.w >= inner.x + inner.w
            and outer.y + outer.h >= inner.y + inner.h)


def _normalize(rects: Sequence[Union[Rect, Tuple[float, float]]]) -> List[Rect]:
    """统一输入为 Rect 列表并校验。零/负尺寸直接拒绝（ValueError）。"""
    norm: List[Rect] = []
    for i, r in enumerate(rects):
        if isinstance(r, Rect):
            w, h, rid = r.w, r.h, r.rid or f"r{i}"
        else:
            w, h = r
            rid = f"r{i}"
        if w <= 0 or h <= 0:
            raise ValueError(f"矩形 {rid} 尺寸必须为正数，得到 {w}x{h}")
        norm.append(Rect(w, h, rid))
    return norm


# ---------------------------------------------------------------- MaxRects

def _score(fw: float, fh: float, rw: float, rh: float):
    """BSSF 评分：短边剩余最小优先，平局比剩余面积。越小越好。"""
    leftover_h = fw - rw
    leftover_v = fh - rh
    short_side = min(leftover_h, leftover_v)
    area_fit = fw * fh - rw * rh
    return (short_side, area_fit)


def _find_best(free: List[_FreeRect], w: float, h: float,
               allow_rotation: bool):
    """在空闲矩形中找最佳位置。返回 (key, free_index, rw, rh, rotated) 或 None。"""
    best = None
    for i, f in enumerate(free):
        orientations = ((w, h, False),)
        if allow_rotation and w != h:
            orientations = ((w, h, False), (h, w, True))
        for rw, rh, rotated in orientations:
            if rw <= f.w and rh <= f.h:
                key = (_score(f.w, f.h, rw, rh), f.y, f.x, i, rotated)
                if best is None or key < best[0]:
                    best = (key, i, rw, rh, rotated)
    return best


def _split_and_prune(free: List[_FreeRect], px: float, py: float,
                     pw: float, ph: float) -> None:
    """对所有与占位相交的空闲矩形做最大切分，并增量剪除被包含者。"""
    kept: List[_FreeRect] = []
    segments: List[_FreeRect] = []
    for f in free:
        if (px >= f.x + f.w or px + pw <= f.x
                or py >= f.y + f.h or py + ph <= f.y):
            kept.append(f)  # 不相交，原样保留
            continue
        if px > f.x:
            segments.append(_FreeRect(f.x, f.y, px - f.x, f.h))
        if px + pw < f.x + f.w:
            segments.append(_FreeRect(px + pw, f.y, f.x + f.w - (px + pw), f.h))
        if py > f.y:
            segments.append(_FreeRect(f.x, f.y, f.w, py - f.y))
        if py + ph < f.y + f.h:
            segments.append(_FreeRect(f.x, py + ph, f.w, f.y + f.h - (py + ph)))
    # 增量剪枝：新段若被已有空闲矩形包含则丢弃；若包含已有者则移除已有者
    for seg in segments:
        if seg.w <= 0 or seg.h <= 0:
            continue
        drop = False
        i = 0
        while i < len(kept):
            g = kept[i]
            if _contains(g, seg):
                drop = True
                break
            if _contains(seg, g):
                kept.pop(i)
                continue
            i += 1
        if not drop:
            kept.append(seg)
    free[:] = kept


def pack(width: float,
         rects: Sequence[Union[Rect, Tuple[float, float]]],
         allow_rotation: bool = False,
         fixed_order: bool = False) -> PackingResult:
    """把 rects 打包进宽为 width 的容器（高度不限），返回 PackingResult。

    参数：
      width          容器宽度，必须为正数。
      rects          Rect 或 (w, h) 元组序列；空序列合法（高度 0，利用率 0）。
      allow_rotation 是否允许旋转 90°；旋转放置的矩形 rotated=True。
      fixed_order    True 时严格按输入顺序放置；False 时按
                     (长边, 短边, 面积, 输入序号) 降序重排以提高利用率。

    确定行为：
      - 宽或高 <= 0 的矩形：抛出 ValueError；
      - 即使旋转后仍宽于容器的矩形：列入 unplaced（reason="exceeds container width"）；
      - 找不到空间的矩形：列入 unplaced（reason="no space left"）；
      - 同一输入多次运行结果完全一致（整数/浮点确定性评分与平局规则）。
    """
    if width <= 0:
        raise ValueError(f"容器宽度必须为正数，得到 {width}")
    norm = _normalize(rects)
    if not norm:
        return PackingResult(width=width, height=0, placed=[], unplaced=[])

    placed: List[PlacedRect] = []
    unplaced: List[UnplacedRect] = []

    # 高度上界：所有矩形叠起来的高度必然够用
    bound = sum(max(r.w, r.h) if allow_rotation else r.h for r in norm) + 1
    free: List[_FreeRect] = [_FreeRect(0, 0, width, bound)]

    if fixed_order:
        order = list(enumerate(norm))
    else:
        # 确定性重排：长边降序 -> 短边降序 -> 面积降序 -> 输入序号升序
        order = sorted(
            enumerate(norm),
            key=lambda t: (-max(t[1].w, t[1].h), -min(t[1].w, t[1].h),
                           -(t[1].w * t[1].h), t[0]),
        )

    for _, rect in order:
        # 连旋转都放不进容器宽度的，直接拒收
        fits_width = rect.w <= width or (allow_rotation and rect.h <= width)
        if not fits_width:
            unplaced.append(UnplacedRect(rect.rid, rect.w, rect.h,
                                         "exceeds container width"))
            continue
        best = _find_best(free, rect.w, rect.h, allow_rotation)
        if best is None:
            unplaced.append(UnplacedRect(rect.rid, rect.w, rect.h,
                                         "no space left"))
            continue
        _, i, rw, rh, rotated = best
        fx, fy = free[i].x, free[i].y
        placed.append(PlacedRect(rect.rid, fx, fy, rw, rh, rotated))
        _split_and_prune(free, fx, fy, rw, rh)

    height = max((p.y + p.h for p in placed), default=0)
    return PackingResult(width=width, height=height,
                         placed=placed, unplaced=unplaced)


# ---------------------------------------------------------------- 朴素基线

def pack_naive(width: float,
               rects: Sequence[Union[Rect, Tuple[float, float]]],
               allow_rotation: bool = False) -> PackingResult:
    """朴素 Shelf（层架）算法：按面积降序，从左到右排，放不下就换行。

    仅用于与 pack() 做利用率对比，接口与返回结构保持一致。
    """
    if width <= 0:
        raise ValueError(f"容器宽度必须为正数，得到 {width}")
    norm = _normalize(rects)
    if not norm:
        return PackingResult(width=width, height=0, placed=[], unplaced=[])

    order = sorted(enumerate(norm),
                   key=lambda t: (-(t[1].w * t[1].h), t[0]))
    placed: List[PlacedRect] = []
    unplaced: List[UnplacedRect] = []
    x = y = 0.0
    shelf_h = 0.0

    for _, rect in order:
        rw, rh, rotated = rect.w, rect.h, False
        if rw > width:
            if allow_rotation and rect.h <= width:
                rw, rh, rotated = rect.h, rect.w, True
            else:
                unplaced.append(UnplacedRect(rect.rid, rect.w, rect.h,
                                             "exceeds container width"))
                continue
        if x + rw > width:
            y += shelf_h
            x = 0.0
            shelf_h = 0.0
        placed.append(PlacedRect(rect.rid, x, y, rw, rh, rotated))
        x += rw
        shelf_h = max(shelf_h, rh)

    height = max((p.y + p.h for p in placed), default=0)
    return PackingResult(width=width, height=height,
                         placed=placed, unplaced=unplaced)
