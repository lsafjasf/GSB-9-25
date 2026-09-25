"""rectclip: 矩形与线段的相交判定与裁剪（仅标准库）。

坐标约定
--------
- 点:   (x, y) 二元组，float。
- 矩形: (xmin, ymin, xmax, ymax)，允许退化（宽或高为 0，甚至两者都为 0 的点矩形）。
  传入前会规范化（min/max 交换），因此允许 xmin > xmax 的输入。
- 线段: 两个端点 (p1, p2)，允许零长度（p1 == p2），视为一个点。

退化情形规则（显式定义，全库一致）
----------------------------------
R1 零长度线段：视为一个点。与矩形相交 <=> 该点在矩形内或边界上（eps 意义下）。
R2 点矩形 / 线矩形：视为普通矩形的退化形式，不做特判分支——
   点在其上即"边界"，不存在"内部"。与线段相交 <=> 线段到该区域的最短距离 <= eps。
R3 线段与矩形边共线重叠：距离为 0，判定为相交；裁剪返回重叠段（保留原方向）。
R4 端点恰好落在角上 / 边上：属于"边界"，判定为相交；裁剪会保留该端点。
R5 相切（线段与矩形仅接触一点，距离恰为 0）：判定为相交；
   裁剪返回一个零长度结果段（t0 == t1），方向信息退化为点。

容差（eps）
-----------
默认 EPS = 1e-9（绝对容差）。依据：布局坐标量级通常在 1e-3 ~ 1e6，
float64 在该量级下的舍入误差约 1e-13 ~ 1e-10，1e-9 既远高于舍入噪声，
又远低于任何有语义的几何间隙（通常 >= 1e-6）。
所有"相交"判定统一采用闭规则：最短距离 <= eps 即相交。
因此对"相切算不算相交"的结论是：算。副作用是距离在 (0, eps] 内的
"近失"也会被算作相切——这是闭规则的固有取舍，调用方可通过调小 eps 收紧。
"""

from enum import Enum

EPS = 1e-9


class PointRectRelation(Enum):
    INSIDE = "inside"      # 严格内部（距每条边都 > eps）
    BOUNDARY = "boundary"  # 在矩形上且距某条边 <= eps（含退化矩形的全部点）
    OUTSIDE = "outside"    # 在矩形外（到矩形的距离 > eps）


def normalize_rect(rect):
    """规范化矩形为 (xmin, ymin, xmax, ymax)，允许任意顺序的输入。"""
    (x1, y1, x2, y2) = rect
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def point_rect_relation(px, py, rect, eps=EPS):
    """点与矩形的关系，返回 PointRectRelation。规则见模块 docstring R2/R4。"""
    xmin, ymin, xmax, ymax = normalize_rect(rect)
    if px < xmin - eps or px > xmax + eps or py < ymin - eps or py > ymax + eps:
        return PointRectRelation.OUTSIDE
    if px < xmin + eps or px > xmax - eps or py < ymin + eps or py > ymax - eps:
        return PointRectRelation.BOUNDARY
    return PointRectRelation.INSIDE


def point_in_rect(px, py, rect, eps=EPS):
    """便捷判定：点在矩形内或边界上（eps 闭规则）。"""
    return point_rect_relation(px, py, rect, eps) is not PointRectRelation.OUTSIDE


def _seg_seg_dist2(p1, p2, p3, p4):
    """两线段最短距离的平方（Ericson, Real-Time Collision Detection 5.1.9）。

    对零长度线段天然成立（退化为点-线段 / 点-点距离）。
    """
    d1x, d1y = p2[0] - p1[0], p2[1] - p1[1]
    d2x, d2y = p4[0] - p3[0], p4[1] - p3[1]
    rx, ry = p1[0] - p3[0], p1[1] - p3[1]
    a = d1x * d1x + d1y * d1y   # |seg1|^2
    e = d2x * d2x + d2y * d2y   # |seg2|^2
    f = d2x * rx + d2y * ry

    if a <= 0.0 and e <= 0.0:          # 两条都是点
        return rx * rx + ry * ry
    if a <= 0.0:                       # seg1 是点
        s = 0.0
        t = min(max(f / e, 0.0), 1.0)
    elif e <= 0.0:                     # seg2 是点
        t = 0.0
        c = d1x * rx + d1y * ry
        s = min(max(-c / a, 0.0), 1.0)
    else:
        c = d1x * rx + d1y * ry
        b = d1x * d2x + d1y * d2y
        denom = a * e - b * b
        s = min(max((b * f - c * e) / denom, 0.0), 1.0) if denom > 0.0 else 0.0
        t = (b * s + f) / e
        if t < 0.0:
            t = 0.0
            s = min(max(-c / a, 0.0), 1.0)
        elif t > 1.0:
            t = 1.0
            s = min(max((b - c) / a, 0.0), 1.0)
    cx = rx + d1x * s - d2x * t
    cy = ry + d1y * s - d2y * t
    return cx * cx + cy * cy


def segments_intersect(p1, p2, p3, p4, eps=EPS):
    """线段与线段是否相交（闭规则：最短距离 <= eps 即相交）。

    统一覆盖：零长度线段（点）、共线重叠、端点相接、T 型相接。
    """
    return _seg_seg_dist2(p1, p2, p3, p4) <= eps * eps


def _rect_edges(rect):
    xmin, ymin, xmax, ymax = rect
    c = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    return [(c[i], c[(i + 1) % 4]) for i in range(4)]


def segment_rect_intersects(p1, p2, rect, eps=EPS):
    """线段与矩形是否相交（含边界接触；含全部退化情形）。

    规则：任一端点在矩形上（R1/R4），或线段与任一条边的距离 <= eps（R3/R5）。
    对退化矩形，边本身退化为点/线，距离判定天然适用（R2）。
    """
    r = normalize_rect(rect)
    if point_in_rect(p1[0], p1[1], r, eps) or point_in_rect(p2[0], p2[1], r, eps):
        return True
    e2 = eps * eps
    for a, b in _rect_edges(r):
        if _seg_seg_dist2(p1, p2, a, b) <= e2:
            return True
    return False


def clip_segment_to_rect(p1, p2, rect, eps=EPS):
    """Liang-Barsky 参数化裁剪。保留原始方向与参数顺序。

    返回 (t0, t1, q0, q1)：
      - 0 <= t0 <= t1 <= 1，为原线段参数区间（p(t) = p1 + t*(p2-p1)）；
      - q0 = p(t0), q1 = p(t1) 为裁剪后端点，q0 -> q1 与原线段同向；
      - 相切时 t0 == t1，q0 == q1（零长度结果，见 R5）；
      - 零长度输入线段在矩形上时返回 (0.0, 1.0, p1, p1)（R1）。
    不相交返回 None。

    eps 仅用于"平行且在窗外"的拒绝判定（容差内视为贴边，不拒绝），
    裁剪边界本身使用精确矩形，保证结果端点严格落在矩形上。
    """
    xmin, ymin, xmax, ymax = normalize_rect(rect)
    x0, y0 = p1
    dx, dy = p2[0] - x0, p2[1] - y0

    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0),
                 (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0.0:
            # 与该对边界平行：在窗外（超出 eps）则确定无交
            if q < -eps:
                return None
        else:
            r = q / p
            if p < 0.0:
                if r > t1:
                    return None
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return None
                if r < t1:
                    t1 = r
    if t0 > t1:
        return None
    q0 = (x0 + t0 * dx, y0 + t0 * dy)
    q1 = (x0 + t1 * dx, y0 + t1 * dy)
    return (t0, t1, q0, q1)
