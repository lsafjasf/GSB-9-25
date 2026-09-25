"""矩形与线段的相交判定与裁剪库（仅标准库）。

约定
----
- 点:   (x, y) 二元组，float。
- 线段: (p, q)，p/q 为点；p == q 时是零长度线段（退化）。
- 矩形: (xmin, ymin, xmax, ymax)，允许退化（零宽、零高、点矩形）。
        构造时自动规范化，保证 xmin <= xmax, ymin <= ymax。

退化规则（显式定义，全库一致）
------------------------------
R1 零长度线段视为一个点：与矩形相交当且仅当该点在矩形内部或边界上（含容差）。
R2 点矩形（xmin==xmax 且 ymin==ymax）视为一个点：线段与之相交当且仅当
   该点落在线段上（含容差）；零宽/零高矩形视为一条线段。
R3 线段与矩形边共线重叠：算相交；裁剪时返回重叠部分（保留原方向）。
R4 端点恰好落在矩形角上：算相交（边界闭集语义）。
R5 相切（线段与矩形仅接触一点/一边，距离在容差内）：算相交。

容差
----
EPS = 1e-9，按坐标量级做相对缩放：tol = EPS * max(1, |coords|)。
取向量（叉积）容差按乘积项量级缩放，保证量纲一致。
含义：距离 <= tol 的"接触"一律判定为相交/在边界上。因此"相切算相交"，
且对接近相切的浮点噪声稳定；需要严格分离语义的调用方应把 EPS 调小。
"""

EPS = 1e-9

INSIDE = "inside"
BOUNDARY = "boundary"
OUTSIDE = "outside"

__all__ = [
    "EPS", "INSIDE", "BOUNDARY", "OUTSIDE",
    "normalize_rect", "point_rect_relation",
    "point_on_segment", "segments_intersect",
    "seg_rect_intersect", "clip_segment_to_rect",
]


def _tol(*vals):
    """按坐标量级缩放的绝对容差。"""
    return EPS * max(1.0, max(abs(v) for v in vals))


def normalize_rect(rect):
    x0, y0, x1, y1 = rect
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def point_rect_relation(px, py, rect):
    """返回 INSIDE / BOUNDARY / OUTSIDE。退化矩形上不存在 INSIDE。"""
    x0, y0, x1, y1 = normalize_rect(rect)
    t = _tol(px, py, x0, y0, x1, y1)
    if px < x0 - t or px > x1 + t or py < y0 - t or py > y1 + t:
        return OUTSIDE
    on_edge = (abs(px - x0) <= t or abs(px - x1) <= t
               or abs(py - y0) <= t or abs(py - y1) <= t)
    return BOUNDARY if on_edge else INSIDE


def _orient(ax, ay, bx, by, cx, cy):
    """叉积 (b-a)x(c-a) 及其容差，返回 (value, tol)。"""
    p1 = (bx - ax) * (cy - ay)
    p2 = (by - ay) * (cx - ax)
    return p1 - p2, EPS * max(1.0, abs(p1), abs(p2))


def _is_zero_len(a, b):
    t = _tol(a[0], a[1], b[0], b[1])
    return abs(a[0] - b[0]) <= t and abs(a[1] - b[1]) <= t


def point_on_segment(p, a, b):
    """点 p 是否在线段 ab 上（含容差，含端点）。"""
    if _is_zero_len(a, b):
        t = _tol(p[0], p[1], a[0], a[1])
        return abs(p[0] - a[0]) <= t and abs(p[1] - a[1]) <= t
    v, t = _orient(a[0], a[1], b[0], b[1], p[0], p[1])
    if abs(v) > t:
        return False
    tt = _tol(p[0], p[1], a[0], a[1], b[0], b[1])
    return (min(a[0], b[0]) - tt <= p[0] <= max(a[0], b[0]) + tt
            and min(a[1], b[1]) - tt <= p[1] <= max(a[1], b[1]) + tt)


def segments_intersect(seg1, seg2):
    """线段相交判定。共线重叠、端点相接、零长度线段均按规则 R1/R3/R4 处理。"""
    (ax, ay), (bx, by) = seg1
    (cx, cy), (dx, dy) = seg2
    za = _is_zero_len((ax, ay), (bx, by))
    zb = _is_zero_len((cx, cy), (dx, dy))
    if za and zb:
        t = _tol(ax, ay, cx, cy)
        return abs(ax - cx) <= t and abs(ay - cy) <= t
    if za:
        return point_on_segment((ax, ay), (cx, cy), (dx, dy))
    if zb:
        return point_on_segment((cx, cy), (ax, ay), (bx, by))

    o1, t1 = _orient(ax, ay, bx, by, cx, cy)
    o2, t2 = _orient(ax, ay, bx, by, dx, dy)
    o3, t3 = _orient(cx, cy, dx, dy, ax, ay)
    o4, t4 = _orient(cx, cy, dx, dy, bx, by)

    if ((o1 > t1 and o2 < -t2) or (o1 < -t1 and o2 > t2)) and \
       ((o3 > t3 and o4 < -t4) or (o3 < -t3 and o4 > t4)):
        return True
    # 共线/端点接触
    if abs(o1) <= t1 and point_on_segment((cx, cy), (ax, ay), (bx, by)):
        return True
    if abs(o2) <= t2 and point_on_segment((dx, dy), (ax, ay), (bx, by)):
        return True
    if abs(o3) <= t3 and point_on_segment((ax, ay), (cx, cy), (dx, dy)):
        return True
    if abs(o4) <= t4 and point_on_segment((bx, by), (cx, cy), (dx, dy)):
        return True
    return False


def _rect_edges(rect):
    x0, y0, x1, y1 = rect
    return [
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ]


def seg_rect_intersect(p, q, rect):
    """线段 pq 与矩形（闭区域）是否相交。O(1)。"""
    rect = normalize_rect(rect)
    x0, y0, x1, y1 = rect

    # R1: 零长度线段 -> 点判定
    if _is_zero_len(p, q):
        return point_rect_relation(p[0], p[1], rect) != OUTSIDE

    # R2: 退化矩形
    t = _tol(x0, y0, x1, y1)
    if abs(x1 - x0) <= t and abs(y1 - y0) <= t:
        return point_on_segment((x0, y0), p, q)
    if abs(x1 - x0) <= t or abs(y1 - y0) <= t:
        return segments_intersect((p, q), ((x0, y0), (x1, y1)))

    # 端点在矩形内（含边界）
    if point_rect_relation(p[0], p[1], rect) != OUTSIDE:
        return True
    if point_rect_relation(q[0], q[1], rect) != OUTSIDE:
        return True

    # 与四条边相交
    for e in _rect_edges(rect):
        if segments_intersect((p, q), e):
            return True
    return False


def clip_segment_to_rect(p, q, rect):
    """Liang-Barsky 裁剪。返回 ((x0,y0),(x1,y1),t0,t1) 或 None。

    - 保留原始方向：返回的线段方向与 p->q 一致，t0 <= t1 为原线段参数。
    - 零长度线段：在矩形内则原样返回（t0=0, t1=1），否则 None。
    - 退化矩形（零宽/零高/点）：按 R2 语义处理，由同一参数化裁剪自然支持。
    """
    rect = normalize_rect(rect)
    x0, y0, x1, y1 = rect
    px, py = p
    qx, qy = q
    dx = qx - px
    dy = qy - py

    t0, t1 = 0.0, 1.0
    # (p_i, q_i): p_i * t <= q_i
    for pi, qi in ((-dx, px - x0), (dx, x1 - px),
                   (-dy, py - y0), (dy, y1 - py)):
        t = _tol(px, py, qx, qy, x0, y0, x1, y1)
        if pi == 0.0:
            if qi < -t:
                return None  # 平行且在边界外
            continue
        r = qi / pi
        if pi < 0:
            if r > t1 + EPS:
                return None
            if r > t0:
                t0 = r
        else:
            if r < t0 - EPS:
                return None
            if r < t1:
                t1 = r
    if t0 > t1 + EPS:
        return None
    t0 = min(max(t0, 0.0), 1.0)
    t1 = min(max(t1, 0.0), 1.0)
    if t0 > t1:
        return None
    return ((px + t0 * dx, py + t0 * dy),
            (px + t1 * dx, py + t1 * dy),
            t0, t1)
