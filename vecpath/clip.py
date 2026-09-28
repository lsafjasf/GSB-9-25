"""矩形裁剪与路径数据序列化。

clip_path:    用轴对齐矩形裁剪段序列, 只保留矩形内的部分。
              曲线与矩形边界的交点由解析求根 (二次/三次方程) 确定,
              裁剪片段经 de Casteljau 分割提取, 是原曲线的精确子段,
              不引入形状近似。因此: 裁剪后总弧长 <= 原路径总弧长,
              裁剪后包围盒 ⊆ 裁剪矩形 ∩ 原包围盒 (求根精度 ~1e-12)。
to_path_data: 段序列 -> SVG 路径数据字符串, 可被 parser.parse 重新解析。
"""

import math
from typing import List, Tuple

from .parser import Point, Segment
from .geometry import point_at, split_segment

Rect = Tuple[float, float, float, float]  # (minx, miny, maxx, maxy)

_DEDUP_EPS = 1e-12  # 交点参数去重/端点吸附阈值


# ---------- 多项式求根 (用于曲线与矩形边界的交点) ----------

def _roots_quad(c2: float, c1: float, c0: float) -> List[float]:
    """c2 t^2 + c1 t + c0 = 0 的全部实根。"""
    if c2 == 0.0:
        if c1 == 0.0:
            return []
        return [-c0 / c1]
    disc = c1 * c1 - 4.0 * c2 * c0
    if disc < 0.0:
        return []
    if disc == 0.0:
        return [-c1 / (2.0 * c2)]
    sq = math.sqrt(disc)
    # 数值稳定形式, 避免相消 (此时 q 必不为 0)
    q = -0.5 * (c1 + math.copysign(sq, c1))
    return [q / c2, c0 / q]


def _cbrt(x: float) -> float:
    return math.copysign(abs(x) ** (1.0 / 3.0), x)


def _roots_cubic(c3: float, c2: float, c1: float, c0: float) -> List[float]:
    """c3 t^3 + c2 t^2 + c1 t + c0 = 0 的全部实根 (解析解 + Newton 精化)。"""
    if c3 == 0.0:
        return _roots_quad(c2, c1, c0)
    a, b, c = c2 / c3, c1 / c3, c0 / c3
    # 退化三次 u^3 + p u + q = 0, t = u - a/3
    p = b - a * a / 3.0
    q = a * (2.0 * a * a - 9.0 * b) / 27.0 + c
    disc = 0.25 * q * q + (p / 3.0) ** 3
    if disc > 0.0:
        sq = math.sqrt(disc)
        roots = [_cbrt(-0.5 * q + sq) + _cbrt(-0.5 * q - sq) - a / 3.0]
    elif p == 0.0:
        roots = [-a / 3.0]  # 三重根
    else:
        # 三实根情形: 三角法 (数值稳定)
        m = 2.0 * math.sqrt(-p / 3.0)
        arg = 1.5 * q / p * math.sqrt(-3.0 / p)
        theta = math.acos(max(-1.0, min(1.0, arg))) / 3.0
        roots = [m * math.cos(theta - 2.0 * math.pi * k / 3.0) - a / 3.0
                 for k in range(3)]
    # 在原多项式上做 Newton 精化, 抵消中间变形的舍入误差
    refined = []
    for t in roots:
        for _ in range(4):
            f = ((c3 * t + c2) * t + c1) * t + c0
            df = (3.0 * c3 * t + 2.0 * c2) * t + c1
            if df == 0.0:
                break
            t -= f / df
        refined.append(t)
    return refined


# ---------- 曲线与矩形边界的交点参数 ----------

def _crossing_params(seg: Segment, rect: Rect) -> List[float]:
    """段与矩形四边交点的参数 t 列表 (仅保留 (0,1) 内部, 升序)。"""
    minx, miny, maxx, maxy = rect
    ts: List[float] = []
    if seg.kind in ("L", "Z"):
        (x0, y0), (x1, y1) = seg.points
        if x0 != x1:
            ts += [(v - x0) / (x1 - x0) for v in (minx, maxx)]
        if y0 != y1:
            ts += [(v - y0) / (y1 - y0) for v in (miny, maxy)]
    elif seg.kind == "Q":
        p0, p1, p2 = seg.points
        for axis in (0, 1):
            a0, a1, a2 = p0[axis], p1[axis], p2[axis]
            for v in ((minx, maxx) if axis == 0 else (miny, maxy)):
                # a(t) = v: (a0-2a1+a2) t^2 + 2(a1-a0) t + (a0-v) = 0
                ts += _roots_quad(a0 - 2.0 * a1 + a2, 2.0 * (a1 - a0), a0 - v)
    elif seg.kind == "C":
        p0, p1, p2, p3 = seg.points
        for axis in (0, 1):
            a0, a1, a2, a3 = (p0[axis], p1[axis], p2[axis], p3[axis])
            for v in ((minx, maxx) if axis == 0 else (miny, maxy)):
                # 伯恩斯坦形式展开为幂基后求解 a(t) = v
                ts += _roots_cubic(-a0 + 3.0 * a1 - 3.0 * a2 + a3,
                                   3.0 * a0 - 6.0 * a1 + 3.0 * a2,
                                   -3.0 * a0 + 3.0 * a1,
                                   a0 - v)
    else:
        raise ValueError(f"未知段类型: {seg.kind!r}")
    return sorted(t for t in ts if _DEDUP_EPS < t < 1.0 - _DEDUP_EPS)


def _extract(seg: Segment, t0: float, t1: float) -> Segment:
    """提取段在参数区间 [t0,t1] 上的精确子段 (0 <= t0 < t1 <= 1)。"""
    if t0 <= 0.0 and t1 >= 1.0:
        return seg
    left = split_segment(seg, t1)[0] if t1 < 1.0 else seg
    if t0 > 0.0:
        return split_segment(left, t0 / t1)[1]
    return left


# ---------- 裁剪 ----------

def clip_path(segments: List[Segment], rect: Rect) -> List[Segment]:
    """用矩形 (minx, miny, maxx, maxy) 裁剪段序列, 返回新的段序列。

    输出仅含 M/L/Q/C 段 (Z 按等价直线参与裁剪); 每个保留片段完整落在
    矩形内, 且是原曲线的精确子段。片段起点与上一片段终点不衔接时插入
    M 段。空结果表示路径完全在矩形外。
    """
    minx, miny, maxx, maxy = rect
    if not (minx <= maxx and miny <= maxy):
        raise ValueError(f"非法矩形: {rect!r}")
    scale = max(1.0, abs(minx), abs(miny), abs(maxx), abs(maxy))
    eps = 1e-9 * scale  # 中点归属判定的浮点余量
    out: List[Segment] = []
    pen: Point = None  # 输出序列的当前笔位
    for seg in segments:
        if seg.kind == "M":
            pen = None
            continue
        base = Segment("L", seg.points) if seg.kind == "Z" else seg
        cuts = [0.0, 1.0] + _crossing_params(base, rect)
        cuts.sort()
        kept: List[float] = []
        for t in cuts:
            if not kept or t - kept[-1] > _DEDUP_EPS:
                kept.append(t)
        for i in range(len(kept) - 1):
            t0, t1 = kept[i], kept[i + 1]
            x, y = point_at(base, (t0 + t1) * 0.5)
            inside = (minx - eps <= x <= maxx + eps
                      and miny - eps <= y <= maxy + eps)
            if not inside:
                pen = None
                continue
            piece = _extract(base, t0, t1)
            if pen != piece.points[0]:
                out.append(Segment("M", (piece.points[0],)))
            out.append(piece)
            pen = piece.points[-1]
    return out


# ---------- 序列化 ----------

def to_path_data(segments: List[Segment]) -> str:
    """把段序列序列化为 SVG 路径数据字符串 (绝对坐标)。

    浮点数用 repr 最短往返格式, 因此对 parse 产生的段序列精确成立
    parse(to_path_data(segs)) == segs。段的起点由前一段笔位隐含, 只输出
    终点与控制点 (否则 L/Q/C 的多余数字会被解析为隐式重复参数组);
    这要求段序列笔位连续 (parse / clip_path 的输出均满足)。
    """
    parts: List[str] = []
    for seg in segments:
        if seg.kind == "Z":
            parts.append("Z")
        else:
            pts = seg.points if seg.kind == "M" else seg.points[1:]
            nums = " ".join(repr(v) for p in pts for v in p)
            parts.append(f"{seg.kind} {nums}")
    return " ".join(parts)
