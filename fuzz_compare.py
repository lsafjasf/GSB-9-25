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


if __name__ == "__main__":
    main()
