"""对拍：精确算法 vs 逐点采样近似法。

采样近似法：把线段均匀切成 N 段，逐点判定是否在矩形内（含边界，同容差）。
性质：采样法只会漏报（false negative），不会误报 —— 采样点命中即真实相交。

硬性要求（本脚本断言）：
  A. 采样判定为相交的用例，精确算法绝不允许判为不相交（零漏报）。
  B. 精确算法判定为相交时，采样最小距离必须足够小（量级 ~len/N，排除误报）。
  C. 裁剪一致性：相交 <=> 裁剪非空；裁剪结果在矩形内、是原线段的子集、
     方向一致；采样命中点的参数 t 必须落在裁剪参数区间 [t0, t1] 内（含容差）。
"""
import math
import random

from rect_seg import (
    EPS, OUTSIDE, point_rect_relation, seg_rect_intersect, clip_segment_to_rect,
)

SAMPLES = 4000          # 每条线段采样点数
N_CASES = 30000         # 随机用例总数
RECT = (0.0, 0.0, 10.0, 10.0)


def sample_hits(p, q, rect, n=SAMPLES):
    """返回 (是否命中, 命中点的最小/最大参数 t, 采样点到矩形边界盒的最小距离)。"""
    px, py = p
    dx, dy = q[0] - px, q[1] - py
    x0, y0, x1, y1 = rect
    hit = False
    t_lo, t_hi = math.inf, -math.inf
    min_dist = math.inf
    for i in range(n + 1):
        t = i / n
        x, y = px + t * dx, py + t * dy
        if point_rect_relation(x, y, rect) != OUTSIDE:
            hit = True
            t_lo = min(t_lo, t)
            t_hi = max(t_hi, t)
        # 点到矩形（闭区域）的距离
        ddx = max(x0 - x, 0.0, x - x1)
        ddy = max(y0 - y, 0.0, y - y1)
        d = math.hypot(ddx, ddy)
        if d < min_dist:
            min_dist = d
    return hit, t_lo, t_hi, min_dist


def gen_case(rng, kind):
    if kind == 0:      # 均匀随机
        p = (rng.uniform(-20, 30), rng.uniform(-20, 30))
        q = (rng.uniform(-20, 30), rng.uniform(-20, 30))
    elif kind == 1:    # 端点贴边抖动（考验容差与相切）
        edge = rng.choice([0.0, 10.0])
        j = lambda: rng.uniform(-1e-7, 1e-7)
        p = (edge + j(), rng.uniform(-5, 15))
        q = (rng.uniform(-20, 30), rng.uniform(-20, 30))
    elif kind == 2:    # 角点附近
        c = rng.choice([(0, 0), (0, 10), (10, 0), (10, 10)])
        p = (c[0] + rng.uniform(-1, 1), c[1] + rng.uniform(-1, 1))
        q = (c[0] + rng.uniform(-1, 1), c[1] + rng.uniform(-1, 1))
    elif kind == 3:    # 水平/垂直（共线高发）
        if rng.random() < 0.5:
            y = rng.choice([0.0, 10.0, rng.uniform(-5, 15)])
            p = (rng.uniform(-20, 30), y)
            q = (rng.uniform(-20, 30), y)
        else:
            x = rng.choice([0.0, 10.0, rng.uniform(-5, 15)])
            p = (x, rng.uniform(-20, 30))
            q = (x, rng.uniform(-20, 30))
    elif kind == 4:    # 零长度线段
        p = (rng.uniform(-5, 15), rng.uniform(-5, 15))
        q = p
    else:              # 大坐标（考验相对容差）
        s = 1e6
        p = (rng.uniform(-s, s), rng.uniform(-s, s))
        q = (rng.uniform(-s, s), rng.uniform(-s, s))
    return p, q


def main():
    rng = random.Random(20260925)
    rects = [RECT,
             (5.0, 5.0, 5.0, 5.0),      # 点矩形
             (3.0, 0.0, 3.0, 10.0),     # 零宽
             (0.0, 4.0, 10.0, 4.0)]     # 零高
    stats = {"hit": 0, "miss": 0}
    for i in range(N_CASES):
        p, q = gen_case(rng, i % 6)
        rect = rects[i % len(rects)]

        hit, t_lo, t_hi, min_dist = sample_hits(p, q, rect)
        exact = seg_rect_intersect(p, q, rect)
        clip = clip_segment_to_rect(p, q, rect)

        # A. 采样命中 => 精确必须命中（零漏报）
        assert not (hit and not exact), f"漏报 #{i}: {p} {q} {rect}"

        # B. 精确命中 => 采样最小距离必须足够小（排除明显误报）
        if exact:
            seg_len = math.hypot(q[0] - p[0], q[1] - p[1])
            lim = seg_len / SAMPLES * 2 + EPS * max(1.0, seg_len) * 4
            assert min_dist <= lim, f"误报 #{i}: {p} {q} {rect} d={min_dist}"

        # C. 裁剪一致性
        assert exact == (clip is not None), f"裁剪不一致 #{i}: {p} {q} {rect}"
        if clip is not None:
            a, b, t0, t1 = clip
            assert -EPS <= t0 <= t1 <= 1 + EPS, f"参数越界 #{i}"
            for pt in (a, b):
                assert point_rect_relation(pt[0], pt[1], rect) != OUTSIDE, \
                    f"裁剪端点在矩形外 #{i}: {pt} {rect}"
            # 方向一致：裁剪后向量与原向量同向（点积 >= 0）
            dx, dy = q[0] - p[0], q[1] - p[1]
            dot = (b[0] - a[0]) * dx + (b[1] - a[1]) * dy
            assert dot >= -EPS, f"方向翻转 #{i}"
            # 采样命中参数必须落在裁剪参数区间内（含采样步长容差）
            if hit:
                slack = 1.0 / SAMPLES + EPS
                assert t_lo >= t0 - slack and t_hi <= t1 + slack, \
                    f"裁剪区间不覆盖采样命中 #{i}: [{t0},{t1}] vs [{t_lo},{t_hi}]"

        stats["hit" if exact else "miss"] += 1

    print(f"OK: {N_CASES} 个用例全部通过 "
          f"(相交 {stats['hit']}, 不相交 {stats['miss']}, "
          f"矩形形态 {len(rects)} 种, 采样点/线段 {SAMPLES})")


if __name__ == "__main__":
    main()
