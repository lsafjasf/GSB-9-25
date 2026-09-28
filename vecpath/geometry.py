"""路径几何计算: 精确包围盒与自适应细分长度。

包围盒: 对每段曲线求导数零点(解析解), 端点加极值点取 min/max,
        结果对真实曲线是精确的, 严格不低估。
长度:   自适应二分细分。对每片叶子, 弦长 <= 真实弧长 <= 控制多边形长,
        细分到 (控制多边形长 - 弦长) <= tol 后取两者均值,
        该片误差 <= (控制多边形长 - 弦长) / 2。误差界逐片累加, 与估值一同返回。
"""

import math
from typing import List, Optional, Tuple

from .parser import Point, Segment

BBox = Tuple[float, float, float, float]  # (minx, miny, maxx, maxy)

_MAX_DEPTH = 40  # 细分深度上限; 到达上限仍返回, 误差界依然成立


# ---------- 求值 ----------

def point_at(seg: Segment, t: float) -> Point:
    """返回段上参数 t ∈ [0,1] 处的点。M 段忽略 t 返回其点。"""
    pts = seg.points
    if seg.kind == "M":
        return pts[0]
    if seg.kind in ("L", "Z"):
        (x0, y0), (x1, y1) = pts
        return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
    if seg.kind == "Q":
        (x0, y0), (x1, y1), (x2, y2) = pts
        u = 1.0 - t
        return (u * u * x0 + 2 * u * t * x1 + t * t * x2,
                u * u * y0 + 2 * u * t * y1 + t * t * y2)
    if seg.kind == "C":
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
        u = 1.0 - t
        return (u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3)
    raise ValueError(f"未知段类型: {seg.kind!r}")


# ---------- 包围盒 ----------

def _quad_extrema(a0: float, a1: float, a2: float) -> List[float]:
    """二次贝塞尔单轴导数零点: (a0-a1) + (a0-2a1+a2) t = 0。"""
    denom = a0 - 2.0 * a1 + a2
    if denom == 0.0:
        return []
    t = (a0 - a1) / denom
    return [t] if 0.0 < t < 1.0 else []


def _cubic_extrema(a0: float, a1: float, a2: float, a3: float) -> List[float]:
    """三次贝塞尔单轴导数零点: A t^2 + B t + C = 0 (已约去因子 3)。"""
    A = -a0 + 3.0 * a1 - 3.0 * a2 + a3
    B = 2.0 * (a0 - 2.0 * a1 + a2)
    C = a1 - a0
    if A == 0.0:
        if B == 0.0:
            return []
        t = -C / B
        return [t] if 0.0 < t < 1.0 else []
    disc = B * B - 4.0 * A * C
    if disc <= 0.0:
        return []
    sq = math.sqrt(disc)
    return [t for t in ((-B + sq) / (2.0 * A), (-B - sq) / (2.0 * A))
            if 0.0 < t < 1.0]


def segment_bbox(seg: Segment) -> BBox:
    """单段的精确包围盒 (含端点与导数极值点)。"""
    if seg.kind == "M":
        x, y = seg.points[0]
        return (x, y, x, y)
    coords = [(p[0], p[1]) for p in (seg.points[0], seg.points[-1])]
    ts: List[float] = []
    if seg.kind == "Q":
        p0, p1, p2 = seg.points
        ts = (_quad_extrema(p0[0], p1[0], p2[0])
              + _quad_extrema(p0[1], p1[1], p2[1]))
    elif seg.kind == "C":
        p0, p1, p2, p3 = seg.points
        ts = (_cubic_extrema(p0[0], p1[0], p2[0], p3[0])
              + _cubic_extrema(p0[1], p1[1], p2[1], p3[1]))
    for t in ts:
        coords.append(point_at(seg, t))
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return (min(xs), min(ys), max(xs), max(ys))


def path_bbox(segments: List[Segment]) -> Optional[BBox]:
    """整条路径的包围盒。空路径返回 None; 仅含移动指令时退化为点盒。"""
    box: Optional[BBox] = None
    for seg in segments:
        sb = segment_bbox(seg)
        if box is None:
            box = sb
        else:
            box = (min(box[0], sb[0]), min(box[1], sb[1]),
                   max(box[2], sb[2]), max(box[3], sb[3]))
    return box


# ---------- 长度 ----------

def _dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _split_cubic(p0, p1, p2, p3):
    """de Casteljau 在 t=0.5 处二分三次贝塞尔。"""
    def mid(a, b):
        return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
    q0, q1, q2 = mid(p0, p1), mid(p1, p2), mid(p2, p3)
    r0, r1 = mid(q0, q1), mid(q1, q2)
    m = mid(r0, r1)
    return (p0, q0, r0, m), (m, r1, q2, p3)


def _cubic_length(p0, p1, p2, p3, tol: float, depth: int) -> Tuple[float, float]:
    chord = _dist(p0, p3)
    poly = _dist(p0, p1) + _dist(p1, p2) + _dist(p2, p3)
    if poly - chord <= tol or depth >= _MAX_DEPTH:
        return (poly + chord) * 0.5, (poly - chord) * 0.5
    left, right = _split_cubic(p0, p1, p2, p3)
    ll, le = _cubic_length(*left, tol * 0.5, depth + 1)
    rl, re = _cubic_length(*right, tol * 0.5, depth + 1)
    return ll + rl, le + re


def segment_length(seg: Segment, tol: float = 1e-6) -> Tuple[float, float]:
    """返回 (长度估值, 误差上界)。直线/移动/闭合段误差为 0。"""
    if seg.kind == "M":
        return 0.0, 0.0
    if seg.kind in ("L", "Z"):
        return _dist(seg.points[0], seg.points[1]), 0.0
    if seg.kind == "Q":
        # 二次升阶为三次: c1 = p0 + 2/3(p1-p0), c2 = p2 + 2/3(p1-p2)
        p0, p1, p2 = seg.points
        c1 = (p0[0] + 2.0 / 3.0 * (p1[0] - p0[0]),
              p0[1] + 2.0 / 3.0 * (p1[1] - p0[1]))
        c2 = (p2[0] + 2.0 / 3.0 * (p1[0] - p2[0]),
              p2[1] + 2.0 / 3.0 * (p1[1] - p2[1]))
        return _cubic_length(p0, c1, c2, p2, tol, 0)
    if seg.kind == "C":
        return _cubic_length(*seg.points, tol, 0)
    raise ValueError(f"未知段类型: {seg.kind!r}")


def path_length(segments: List[Segment], tol: float = 1e-6) -> Tuple[float, float]:
    """整条路径长度, 返回 (估值, 误差上界)。"""
    total, err = 0.0, 0.0
    for seg in segments:
        l, e = segment_length(seg, tol)
        total += l
        err += e
    return total, err


# ---------- 细分 ----------

def _lerp(a: Point, b: Point, t: float) -> Point:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def split_segment(seg: Segment, t: float) -> Tuple[Segment, Segment]:
    """在参数 t ∈ [0,1] 处把段分成左右两段 (de Casteljau 分割)。

    左段对应原参数区间 [0,t], 右段对应 [t,1], 两段均为原曲线的精确子段
    (不引入近似)。Z 段按等价直线处理, 返回的两段 kind 均为 'L'。
    """
    if not 0.0 <= t <= 1.0:
        raise ValueError(f"细分参数超出 [0,1]: {t}")
    if seg.kind == "M":
        raise ValueError("M 段没有几何延展, 不可细分")
    if seg.kind in ("L", "Z"):
        p0, p1 = seg.points
        m = _lerp(p0, p1, t)
        return Segment("L", (p0, m)), Segment("L", (m, p1))
    if seg.kind == "Q":
        p0, p1, p2 = seg.points
        a, b = _lerp(p0, p1, t), _lerp(p1, p2, t)
        m = _lerp(a, b, t)
        return Segment("Q", (p0, a, m)), Segment("Q", (m, b, p2))
    if seg.kind == "C":
        p0, p1, p2, p3 = seg.points
        a, b, c = _lerp(p0, p1, t), _lerp(p1, p2, t), _lerp(p2, p3, t)
        d, e = _lerp(a, b, t), _lerp(b, c, t)
        m = _lerp(d, e, t)
        return Segment("C", (p0, a, d, m)), Segment("C", (m, e, c, p3))
    raise ValueError(f"未知段类型: {seg.kind!r}")


# ---------- 弧长参数化 ----------

def _partial_length(seg: Segment, t: float, tol: float) -> Tuple[float, float]:
    """段上参数区间 [0,t] 部分的弧长, 返回 (估值, 误差上界)。"""
    if t <= 0.0:
        return 0.0, 0.0
    if t >= 1.0:
        return segment_length(seg, tol)
    left, _ = split_segment(seg, t)
    return segment_length(left, tol)


def _locate_in_segment(seg: Segment, local: float, seg_len: float,
                       tol: float) -> Tuple[float, float]:
    """段内按弧长定位, 返回 (参数 t, 弧长残差上界)。

    曲线段利用"部分弧长关于 t 单调不减"做二分, 直线段直接按比例。
    """
    if seg.kind == "M" or seg_len == 0.0:
        return 0.0, 0.0
    if seg.kind in ("L", "Z"):
        return local / seg_len, 0.0
    if local <= 0.0:
        return 0.0, 0.0
    if local >= seg_len:
        return 1.0, 0.0
    lo, hi = 0.0, 1.0
    lo_len, hi_len = 0.0, seg_len
    perr = 0.0  # 二分过程中部分弧长估值的最大报告误差
    for _ in range(60):
        if hi_len - lo_len <= tol:
            break
        mid = (lo + hi) * 0.5
        ml, me = _partial_length(seg, mid, tol)
        perr = max(perr, me)
        if ml < local:
            lo, lo_len = mid, ml
        else:
            hi, hi_len = mid, ml
    return (lo + hi) * 0.5, (hi_len - lo_len) * 0.5 + perr


def point_at_length(segments: List[Segment], s: float,
                    tol: float = 1e-6) -> Tuple[Point, float]:
    """按弧长定位: 返回距路径起点弧长 s 处的点与弧长误差上界 (point, err)。

    err 为严格上界: 各段长度估值误差 (逐段报告界累加) 与段内二分定位
    残差之和, 即返回点的真实弧长与 s 之差不超过 err。
    s 超出 [0, 总长 ± 总长误差] 时抛 ValueError。
    """
    if not segments:
        raise ValueError("空路径没有弧长定位点")
    total, terr = path_length(segments, tol)
    if s < -terr or s > total + terr:
        raise ValueError(f"s={s} 超出路径弧长范围 [0, {total} ± {terr}]")
    s = min(max(s, 0.0), total)
    acc = 0.0
    last = len(segments) - 1
    for idx, seg in enumerate(segments):
        seg_len, _ = segment_length(seg, tol)
        if s <= acc + seg_len or idx == last:
            local = min(max(s - acc, 0.0), seg_len)
            t, resid = _locate_in_segment(seg, local, seg_len, tol)
            return point_at(seg, t), terr + resid
        acc += seg_len
    raise AssertionError("不可达: 前面已按总长夹紧 s")
