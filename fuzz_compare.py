"""对拍脚本：rectclip 的 float+eps 实现 vs 两路独立参考。

参考 1 —— 精确分数算术（fractions.Fraction，零舍入误差，无 eps）：
  用精确 orientation / 包围盒测试重写 线段-线段 与 线段-矩形 判定，
  作为 float 实现的真值。随机坐标取整数 [-M, M]：
  真正不相交但距离 > 0 的用例，距离下界约为 1/线段长（>= 2.5e-4，M=2000），
  远大于 eps=1e-9，因此"闭规则带来的 eps 邻域"不会制造任何分歧。

参考 2 —— 逐点采样近似（单向验证）：
  沿线段均匀采样 N 个点（含端点），只要存在严格位于矩形内部
  （距边界留 1e-6 安全边距，避免采样本身的浮点误差）的点就断言"真实相交"。
  采样可能漏（相切、薄穿越），所以只验证一个方向：
     采样说相交  =>  精确实现必须说相交
  即"真实相交的用例不允许被判为不相交"。

同时校验裁剪：相交 <=> clip 返回非 None；裁剪结果的参数恒等性、
参数顺序（t0 <= t1）、结果端点落在矩形上、方向跟随原线段。

分类对拍（classify_*）：
  用 Fraction 精确算术独立重写同一套分类规则（PROPER / ENDPOINT_TOUCH /
  COLLINEAR_OVERLAP / TANGENT / DISJOINT），逐例比较类别与参数区间；
  并校验参数区间可复算：q == p1 + t*(p2-p1)，且 t 能由 q 反投影还原。
  整数坐标下精确算术无舍入，float 实现的 eps 邻域（1e-9）与真实几何
  间隙（>= 2.5e-4）不重叠，因此两套实现必须逐例一致。

用法: python3 fuzz_compare.py [用例数，默认 30000]
"""

import random
import sys
from fractions import Fraction

import rectclip as rc

M = 2000
SAMPLE_N = 256
SAMPLE_MARGIN = 1e-6
CASES = int(sys.argv[1]) if len(sys.argv) > 1 else 30000


# ---------------- 精确 Fraction 参考实现 ----------------

def _F(p):
    return (Fraction(p[0]), Fraction(p[1]))


def orient(a, b, c):
    """精确叉积 (b-a) x (c-a)，返回 Fraction。"""
    return ((b[0] - a[0]) * (c[1] - a[1])
            - (b[1] - a[1]) * (c[0] - a[0]))


def _bbox_overlap(a, b, c, d):
    return (max(min(a[0], b[0]), min(c[0], d[0]))
            <= min(max(a[0], b[0]), max(c[0], d[0])) and
            max(min(a[1], b[1]), min(c[1], d[1]))
            <= min(max(a[1], b[1]), max(c[1], d[1])))


def _on(a, b, p):
    """p 已与 ab 共线时，判断是否落在 ab 线段上（含端点）。"""
    return (min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and
            min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))


def seg_seg_exact(p1, p2, p3, p4):
    """精确线段相交（闭规则：端点接触、共线重叠都算）。"""
    a, b, c, d = _F(p1), _F(p2), _F(p3), _F(p4)
    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    if ((o1 > 0 > o2 or o1 < 0 < o2) and
            (o3 > 0 > o4 or o3 < 0 < o4)):
        return True
    # 退化判定必须逐点验证落在"具体那条线段"上，仅共线 + 包围盒不够
    if o1 == 0 and _on(a, b, c):
        return True
    if o2 == 0 and _on(a, b, d):
        return True
    if o3 == 0 and _on(c, d, a):
        return True
    if o4 == 0 and _on(c, d, b):
        return True
    return False


def point_in_rect_exact(px, py, r):
    xmin, ymin, xmax, ymax = r
    return xmin <= px <= xmax and ymin <= py <= ymax


def seg_rect_exact(p1, p2, rect):
    r = rc.normalize_rect(rect)
    R = tuple(Fraction(v) for v in r)
    a, b = _F(p1), _F(p2)
    if (point_in_rect_exact(a[0], a[1], R) or
            point_in_rect_exact(b[0], b[1], R)):
        return True
    xmin, ymin, xmax, ymax = R
    corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    for i in range(4):
        c, d = corners[i], corners[(i + 1) % 4]
        if seg_seg_exact(a, b, c, d):
            return True
    return False


# ---------------- 精确 Fraction 分类参考 ----------------

def _clip_exact(p1, p2, rect):
    """精确 Liang-Barsky（Fraction，无 eps）。返回 (t0, t1) 或 None。"""
    xmin, ymin, xmax, ymax = (Fraction(v) for v in rc.normalize_rect(rect))
    x0, y0 = Fraction(p1[0]), Fraction(p1[1])
    dx, dy = Fraction(p2[0]) - x0, Fraction(p2[1]) - y0
    t0, t1 = Fraction(0), Fraction(1)
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0),
                 (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0:
            if q < 0:
                return None
        else:
            r = q / p
            if p < 0:
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
    return t0, t1


def classify_seg_rect_exact(p1, p2, rect):
    """精确分类参考：与 rectclip.classify_segment_rect 同一套规则，
    但全部用 Fraction 精确判定（无 eps）。返回 (kind, t0, t1)。"""
    K = rc.IntersectionKind
    R = tuple(Fraction(v) for v in rc.normalize_rect(rect))
    res = _clip_exact(p1, p2, rect)
    if res is None:
        return K.DISJOINT, None, None
    t0, t1 = res
    x0, y0 = Fraction(p1[0]), Fraction(p1[1])
    dx, dy = Fraction(p2[0]) - x0, Fraction(p2[1]) - y0
    q0 = (x0 + t0 * dx, y0 + t0 * dy)
    q1 = (x0 + t1 * dx, y0 + t1 * dy)

    def strictly_inside(q):
        return R[0] < q[0] < R[2] and R[1] < q[1] < R[3]

    if dx == 0 and dy == 0:                       # D1 零长度线段
        return (K.PROPER if strictly_inside(q0) else K.ENDPOINT_TOUCH), t0, t1
    if t1 > t0:                                   # 正长度交集
        mid = ((q0[0] + q1[0]) / 2, (q0[1] + q1[1]) / 2)
        return (K.PROPER if strictly_inside(mid)
                else K.COLLINEAR_OVERLAP), t0, t1
    if t0 == 0 or t0 == 1:                        # 单点接触
        return K.ENDPOINT_TOUCH, t0, t1
    return K.TANGENT, t0, t1


def classify_seg_seg_exact(p1, p2, p3, p4):
    """精确线段-线段分类参考。返回 (kind, t, u)（单点时 t/u 为交点参数，
    共线重叠时为 seg1 上的重叠区间及对应 seg2 区间）。"""
    K = rc.IntersectionKind
    a, b, c, d = _F(p1), _F(p2), _F(p3), _F(p4)
    if not seg_seg_exact(p1, p2, p3, p4):
        return K.DISJOINT, None, None
    d1 = (b[0] - a[0], b[1] - a[1])
    d2 = (d[0] - c[0], d[1] - c[1])
    zero1 = d1 == (0, 0)
    zero2 = d2 == (0, 0)
    if zero1 and zero2:
        return K.ENDPOINT_TOUCH, (Fraction(0),) * 2, (Fraction(0),) * 2

    def param(q, p, dd):
        den = dd[0] * dd[0] + dd[1] * dd[1]
        if den == 0:
            return Fraction(0)
        return ((q[0] - p[0]) * dd[0] + (q[1] - p[1]) * dd[1]) / den

    if zero1 or zero2:
        q = a if zero1 else c
        return (K.ENDPOINT_TOUCH,
                (param(q, a, d1),) * 2, (param(q, c, d2),) * 2)
    cross = d1[0] * d2[1] - d1[1] * d2[0]
    if cross != 0:
        r = (c[0] - a[0], c[1] - a[1])
        t = (r[0] * d2[1] - r[1] * d2[0]) / cross
        u = (r[0] * d1[1] - r[1] * d1[0]) / cross
        if t in (0, 1) or u in (0, 1):
            return K.ENDPOINT_TOUCH, (t, t), (u, u)
        return K.PROPER, (t, t), (u, u)
    # 共线：重叠区间
    ta, tb = sorted((param(c, a, d1), param(d, a, d1)))
    t0, t1 = max(ta, Fraction(0)), min(tb, Fraction(1))
    if t1 > t0:
        q0 = (a[0] + t0 * d1[0], a[1] + t0 * d1[1])
        q1 = (a[0] + t1 * d1[0], a[1] + t1 * d1[1])
        u0, u1 = sorted((param(q0, c, d2), param(q1, c, d2)))
        return K.COLLINEAR_OVERLAP, (t0, t1), (u0, u1)
    t = (t0 + t1) / 2
    q = (a[0] + t * d1[0], a[1] + t * d1[1])
    u = param(q, c, d2)
    return K.ENDPOINT_TOUCH, (t, t), (u, u)


# ---------------- 逐点采样参考（单向） ----------------

def sampling_says_intersect(p1, p2, rect):
    xmin, ymin, xmax, ymax = rc.normalize_rect(rect)
    m = SAMPLE_MARGIN
    for i in range(SAMPLE_N + 1):
        t = i / SAMPLE_N
        x = p1[0] + t * (p2[0] - p1[0])
        y = p1[1] + t * (p2[1] - p1[1])
        if xmin + m < x < xmax - m and ymin + m < y < ymax - m:
            return True
    return False


# ---------------- 随机用例生成（含退化） ----------------

def rand_coord(rng, degenerate_pool):
    # 小整数 + 大整数 + 共享坐标池，制造共线/重合/退化
    if rng.random() < 0.25:
        return rng.choice(degenerate_pool)
    if rng.random() < 0.7:
        return rng.randint(-8, 8)
    return rng.randint(-M, M)


def gen_case(rng, pool):
    def pt():
        return (rand_coord(rng, pool), rand_coord(rng, pool))
    # 矩形（约 15% 概率退化：点矩形或线矩形）
    a, b = pt(), pt()
    roll = rng.random()
    if roll < 0.08:
        b = a                                   # 点矩形
    elif roll < 0.15:
        b = (b[0], a[1]) if rng.random() < 0.5 else (a[0], b[1])  # 线矩形
    rect = (a[0], a[1], b[0], b[1])
    # 线段（约 10% 零长度；常借用已有坐标制造相切/共线）
    if rng.random() < 0.10:
        q = pt()
        p1 = p2 = q
    else:
        if rng.random() < 0.35:
            anchors = [a, b,
                       (a[0], b[1]), (b[0], a[1]),
                       (a[0], 0), (0, a[1])]
            p1 = rng.choice(anchors)
            if rng.random() < 0.5:
                p2 = rng.choice(anchors)
            else:
                p2 = pt()
        else:
            p1, p2 = pt(), pt()
    return p1, p2, rect


# ---------------- 对拍主体 ----------------

def main():
    rng = random.Random(925)
    pool = list(range(-4, 5)) + [M, -M, 0]
    stats = {"cases": 0, "exact_true": 0, "impl_true": 0,
             "sample_true": 0, "zero_seg": 0, "degenerate_rect": 0}
    kind_dist = {k: 0 for k in rc.IntersectionKind}

    for n in range(1, CASES + 1):
        p1, p2, rect = gen_case(rng, pool)
        r = rc.normalize_rect(rect)
        if p1 == p2:
            stats["zero_seg"] += 1
        if r[0] == r[2] or r[1] == r[3]:
            stats["degenerate_rect"] += 1

        got = rc.segment_rect_intersects(p1, p2, r)
        want = seg_rect_exact(p1, p2, r)
        sample = sampling_says_intersect(p1, p2, r)

        if got != want:
            raise AssertionError(
                "精确对拍分歧:\n"
                f"  seg={p1} -> {p2}\n  rect={r}\n"
                f"  float+eps -> {got}; Fraction 精确 -> {want}")
        if sample and not got:
            raise AssertionError(
                "真实相交被判为不相交（采样发现严格内部点）:\n"
                f"  seg={p1} -> {p2}\n  rect={r}")

        # ---- 分类对拍：float+eps 实现 vs 精确 Fraction 分类参考 ----
        cls = rc.classify_segment_rect(p1, p2, r)
        e_kind, e_t0, e_t1 = classify_seg_rect_exact(p1, p2, r)
        kind_dist[cls.kind] += 1
        if cls.kind is not e_kind:
            raise AssertionError(
                "分类对拍分歧:\n"
                f"  seg={p1} -> {p2}\n  rect={r}\n"
                f"  float+eps -> {cls.kind} ({cls.reason})\n"
                f"  Fraction 精确 -> {e_kind}")
        if cls.kind is rc.IntersectionKind.DISJOINT:
            assert cls.t0 is None and cls.q0 is None
        else:
            # 参数区间与精确参考一致（float 舍入容差 1e-9）
            assert abs(cls.t0 - float(e_t0)) <= 1e-9, (cls.t0, e_t0)
            assert abs(cls.t1 - float(e_t1)) <= 1e-9, (cls.t1, e_t1)
            # 参数区间可复算：t 能由 q 反投影精确还原
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            if dx != 0.0 or dy != 0.0:
                den = dx * dx + dy * dy
                for t, q in ((cls.t0, cls.q0), (cls.t1, cls.q1)):
                    t_back = ((q[0] - p1[0]) * dx + (q[1] - p1[1]) * dy) / den
                    assert abs(t_back - t) <= 1e-9, (t_back, t)

        # 裁剪一致性（整数坐标下 eps 邻域不会造成分歧，见模块说明）
        res = rc.clip_segment_to_rect(p1, p2, r)
        if got and res is None:
            raise AssertionError(f"判定相交但裁剪为空: seg={p1}->{p2} rect={r}")
        if not got and res is not None:
            raise AssertionError(f"判定不相交但裁剪非空: seg={p1}->{p2} rect={r}")
        if res is not None:
            t0, t1, q0, q1 = res
            assert 0.0 - rc.EPS <= t0 <= t1 <= 1.0 + rc.EPS, (t0, t1)
            for t, q in ((t0, q0), (t1, q1)):
                for k in (0, 1):
                    assert abs(q[k] - (p1[k] + t * (p2[k] - p1[k]))) <= 1e-7, \
                        (q, t, p1, p2)
                assert r[0] - 1e-9 <= q[0] <= r[2] + 1e-9 and \
                       r[1] - 1e-9 <= q[1] <= r[3] + 1e-9, q
            # 方向跟随：裁剪后线段的朝向与原线段一致（或退化为点）
            dx_o = p2[0] - p1[0]
            dx_c = q1[0] - q0[0]
            assert (dx_o == 0) or (dx_c == 0) or (dx_o * dx_c >= 0)

        stats["cases"] = n
        stats["exact_true"] += int(want)
        stats["impl_true"] += int(got)
        stats["sample_true"] += int(sample)

    print(f"对拍通过: {stats['cases']} 个随机用例（含 {stats['zero_seg']} 个零长度线段, "
          f"{stats['degenerate_rect']} 个退化矩形）")
    print(f"  精确判定相交 {stats['exact_true']}，float 实现判定相交 {stats['impl_true']}（完全一致）")
    print(f"  采样确认严格穿越 {stats['sample_true']} 个，无一被实现漏判")
    print("  线段-矩形分类分布（float 实现与 Fraction 精确参考逐例一致）:")
    for k, v in kind_dist.items():
        print(f"    {k.value:18s} {v}")


def main_seg_seg():
    """线段-线段分类对拍：float+eps vs 精确 Fraction 分类参考。"""
    rng = random.Random(417)
    pool = list(range(-4, 5)) + [M, -M, 0]
    kind_dist = {k: 0 for k in rc.IntersectionKind}
    zero = 0

    for n in range(1, CASES + 1):
        def pt():
            return (rand_coord(rng, pool), rand_coord(rng, pool))
        # 约 10% 零长度线段；常共享端点/共线坐标
        p1, p2 = pt(), pt()
        if rng.random() < 0.10:
            p2 = p1
        roll = rng.random()
        if roll < 0.25 and p1 != p2:
            # 与 seg1 共线：p3/p4 取 seg1 所在直线上的整点
            p3 = p1 if rng.random() < 0.5 else p2
            if rng.random() < 0.5:
                p4 = p1 if p3 == p2 else p2
            else:
                p4 = pt()
                p4 = (p4[0], p3[1]) if p1[1] == p2[1] else \
                     (p3[0], p4[1]) if p1[0] == p2[0] else \
                     (p1[0] + p2[0] - p3[0], p1[1] + p2[1] - p3[1])
        elif roll < 0.65:
            p3 = rng.choice([p1, p2, pt()])
            p4 = p3 if rng.random() < 0.05 else pt()
        else:
            p3 = pt()
            p4 = p3 if rng.random() < 0.05 else pt()
        if p1 == p2 or p3 == p4:
            zero += 1

        cls = rc.classify_segments(p1, p2, p3, p4)
        e_kind, e_t, e_u = classify_seg_seg_exact(p1, p2, p3, p4)
        kind_dist[cls.kind] += 1
        if cls.kind is not e_kind:
            raise AssertionError(
                "线段-线段分类对拍分歧:\n"
                f"  seg1={p1} -> {p2}\n  seg2={p3} -> {p4}\n"
                f"  float+eps -> {cls.kind} ({cls.reason})\n"
                f"  Fraction 精确 -> {e_kind}")
        if cls.kind is not rc.IntersectionKind.DISJOINT:
            # 双侧参数区间与精确参考一致
            for got, want in ((cls.t0, e_t[0]), (cls.t1, e_t[1]),
                              (cls.u0, e_u[0]), (cls.u1, e_u[1])):
                assert abs(got - float(want)) <= 1e-9, (got, want)
            # 参数区间可复算：两侧参数给出同一交点/交段
            # （区间各自升序，反向共线时端点交叉对应，故按点集比较）
            qa = {(p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))
                  for t in (cls.t0, cls.t1)}
            qb = {(p3[0] + u * (p4[0] - p3[0]), p3[1] + u * (p4[1] - p3[1]))
                  for u in (cls.u0, cls.u1)}
            for a in qa:
                assert any(abs(a[0] - b[0]) <= 1e-7 and abs(a[1] - b[1]) <= 1e-7
                           for b in qb), (qa, qb)

    print(f"线段-线段分类对拍通过: {CASES} 个随机用例（含 {zero} 条零长度线段）")
    for k, v in kind_dist.items():
        print(f"    {k.value:18s} {v}")


if __name__ == "__main__":
    main()
    main_seg_seg()
