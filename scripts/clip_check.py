"""裁剪与弧长定位的独立验证 (可复跑, 固定种子)。

对每条 (路径, 矩形) 用例验证:
  1. 可重解析: parse(to_path_data(裁剪结果)) 与裁剪段序列完全一致
  2. 包围盒关系: 裁剪后包围盒 ⊆ 裁剪矩形, 且 ⊆ 原包围盒 (余量 1e-9·scale)
  3. 长度单调: 裁剪后总长 <= 原总长 (计入双方报告误差界)
  4. 形状保持: 裁剪片段采样点都在矩形内, 且落在原路径采样附近
弧长定位: 随机弧长 s 处 point_at_length 与密集采样 (弦长累加+线性插值)
参考点对比, 偏差须 <= 报告误差界 + 采样间距, 并输出实测最大偏差。

用法: python3 scripts/clip_check.py
"""

import math
import random
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vecpath import (
    parse, point_at, path_bbox, path_length,
    point_at_length, clip_path, to_path_data,
)

LEN_TOL = 1e-4        # 长度比较用容差 (误差界仍严格有效)
ARC_TOL = 1e-6        # 弧长定位用容差
ORIG_SAMPLES = 1000   # 原路径每段采样数 (形状保持参照)
CLIP_SAMPLES = 32     # 裁剪片段每段采样数

FIXED_CASES = [
    ("M 0 0 L 10 0 L 10 10 L 0 10 Z", (2.0, 2.0, 8.0, 8.0)),
    ("M 0 0 C 0 3 1 -3 1 0 S 2 -3 2 0 Q 5 5 8 0 L 12 0 Z", (0.5, -1.0, 6.0, 1.5)),
    ("M 0 0 Q 1 2 2 0 T 4 0 T 6 0", (1.0, 0.2, 5.0, 0.8)),
    ("m 3 4 c 1 2 3 -4 5 0 q 2 6 4 0 l -1 -1 z", (2.5, 3.0, 9.0, 6.5)),
    ("M -1e3 2e2 C 3e2 -4e2 5e2 6e2 -7e2 -8e2", (-500.0, -300.0, 400.0, 300.0)),
    ("M 5 5 L 6 6", (0.0, 0.0, 1.0, 1.0)),          # 完全在外 -> 空
    ("M 1 1 C 1 1 1 1 1 1 Z", (0.0, 0.0, 2.0, 2.0)),  # 全退化
]


def random_path(rng: random.Random, n_seg: int) -> str:
    parts = [f"m {rng.uniform(-50, 50):.6f} {rng.uniform(-50, 50):.6f}"]
    for _ in range(n_seg):
        kind = rng.choice("lcq")
        nums = " ".join(f"{rng.uniform(-10, 10):.6f}"
                        for _ in range({"l": 2, "c": 6, "q": 4}[kind]))
        parts.append(f"{kind} {nums}")
    if rng.random() < 0.5:
        parts.append("z")
    return " ".join(parts)


def random_rect(rng: random.Random, box) -> tuple:
    """围绕原包围盒生成随机矩形: 70% 部分相交, 20% 全覆盖, 10% 相离。"""
    minx, miny, maxx, maxy = box
    w, h = maxx - minx, maxy - miny
    r = rng.random()
    if r < 0.2:
        return (minx - 0.1 * w, miny - 0.1 * h, maxx + 0.1 * w, maxy + 0.1 * h)
    if r < 0.3:
        return (maxx + 0.1 * w, maxy + 0.1 * h,
                maxx + 0.5 * w + 1.0, maxy + 0.5 * h + 1.0)
    cx = minx + rng.random() * w
    cy = miny + rng.random() * h
    rw = w * rng.uniform(0.1, 0.9)
    rh = h * rng.uniform(0.1, 0.9)
    return (cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2)


def dense_samples(segs, n_per_seg):
    """每段均匀采样, 返回 (点列表, 弦长累加数组, 最大采样间距)。"""
    pts, cum, gap = [], [0.0], 0.0
    prev = None
    for seg in segs:
        if seg.kind == "M":
            prev = seg.points[0]
            pts.append(prev)
            continue
        for i in range(n_per_seg + 1):
            p = point_at(seg, i / n_per_seg)
            if prev is not None:
                step = math.hypot(p[0] - prev[0], p[1] - prev[1])
                gap = max(gap, step)
                cum.append(cum[-1] + step)
                pts.append(p)
            prev = p
    return pts, cum, gap


def check_clip(d: str, rect: tuple, label: str, stats: dict) -> None:
    segs = parse(d)
    clipped = clip_path(segs, rect)
    scale = max(1.0, *(abs(v) for v in rect))
    eps = 1e-9 * scale

    # 1. 可重解析
    assert parse(to_path_data(clipped)) == clipped, f"[{label}] 重解析不一致"

    obox = path_bbox(segs)
    cbox = path_bbox(clipped)
    lo, eo = path_length(segs, LEN_TOL)
    lc, ec = path_length(clipped, LEN_TOL)

    # 2. 包围盒关系
    if cbox is not None:
        assert cbox[0] >= rect[0] - eps and cbox[2] <= rect[2] + eps, (
            f"[{label}] 裁剪盒越出矩形: {cbox} vs {rect}")
        assert cbox[1] >= rect[1] - eps and cbox[3] <= rect[3] + eps, (
            f"[{label}] 裁剪盒越出矩形: {cbox} vs {rect}")
        assert cbox[0] >= obox[0] - eps and cbox[1] >= obox[1] - eps, (
            f"[{label}] 裁剪盒越出原盒: {cbox} vs {obox}")
        assert cbox[2] <= obox[2] + eps and cbox[3] <= obox[3] + eps, (
            f"[{label}] 裁剪盒越出原盒: {cbox} vs {obox}")

    # 3. 长度单调 (计入双方误差界)
    assert lc <= lo + eo + ec + eps, (
        f"[{label}] 裁剪后变长: {lc} > {lo} (+{eo:.2g}+{ec:.2g})")

    # 4. 形状保持: 裁剪采样点在矩形内, 且落在原路径采样附近
    opts, _, ogap = dense_samples(segs, ORIG_SAMPLES)
    for seg in clipped:
        if seg.kind == "M":
            continue
        for i in range(CLIP_SAMPLES + 1):
            x, y = point_at(seg, i / CLIP_SAMPLES)
            assert rect[0] - eps <= x <= rect[2] + eps, (
                f"[{label}] 裁剪采样点越出矩形 x={x}")
            assert rect[1] - eps <= y <= rect[3] + eps, (
                f"[{label}] 裁剪采样点越出矩形 y={y}")
            best = min(math.hypot(x - px, y - py) for px, py in opts)
            assert best <= ogap + eps, (
                f"[{label}] 裁剪点偏离原路径: dist={best} > 间距={ogap}")

    stats["segs_in"] += len(segs)
    stats["segs_out"] += len(clipped)
    stats["min_len_margin"] = min(stats["min_len_margin"], lo + eo + ec - lc)
    return obox, cbox, (lo, eo), (lc, ec)


def check_arc_length(d: str, label: str, stats: dict, rng: random.Random,
                     n_query: int = 5) -> None:
    segs = parse(d)
    total, terr = path_length(segs, ARC_TOL)
    if total == 0.0:
        return
    pts, cum, gap = dense_samples(segs, ORIG_SAMPLES)
    for _ in range(n_query):
        s = rng.random() * total
        pt, err = point_at_length(segs, s, ARC_TOL)
        # 参考点: 采样弦长序列上按弧长线性插值
        lo_i, hi_i = 0, len(cum) - 1
        while hi_i - lo_i > 1:
            mid = (lo_i + hi_i) // 2
            if cum[mid] <= s:
                lo_i = mid
            else:
                hi_i = mid
        span = cum[hi_i] - cum[lo_i]
        r = 0.0 if span == 0.0 else (s - cum[lo_i]) / span
        ref = (pts[lo_i][0] + (pts[hi_i][0] - pts[lo_i][0]) * r,
               pts[lo_i][1] + (pts[hi_i][1] - pts[lo_i][1]) * r)
        dev = math.hypot(pt[0] - ref[0], pt[1] - ref[1])
        bound = err + gap
        assert dev <= bound + 1e-9, (
            f"[{label}] 弧长定位偏差 {dev:.3g} 超界 {bound:.3g} (s={s:.6f})")
        stats["arc_queries"] += 1
        stats["arc_max_dev"] = max(stats["arc_max_dev"], dev)
        stats["arc_max_bound"] = max(stats["arc_max_bound"], bound)
        stats["arc_max_err"] = max(stats["arc_max_err"], err)


def main() -> None:
    rng = random.Random(20260929)
    stats = {"segs_in": 0, "segs_out": 0, "min_len_margin": math.inf,
             "arc_queries": 0, "arc_max_dev": 0.0,
             "arc_max_bound": 0.0, "arc_max_err": 0.0}

    print("== 固定用例 ==")
    for idx, (d, rect) in enumerate(FIXED_CASES):
        label = f"fixed-{idx}"
        obox, cbox, (lo, eo), (lc, ec) = check_clip(d, rect, label, stats)
        check_arc_length(d, label, stats, rng)
        print(f"[{label}] 矩形={rect}")
        print(f"  包围盒 原={_fmt_box(obox)}")
        print(f"        剪={_fmt_box(cbox)}")
        print(f"  长度   原={lo:.6f}±{eo:.1g}  剪={lc:.6f}±{ec:.1g}")

    n_fuzz = 60
    print(f"== 随机用例 {n_fuzz} 条 (种子 20260929) ==")
    for i in range(n_fuzz):
        d = random_path(rng, rng.randint(1, 30))
        box = path_bbox(parse(d))
        rect = random_rect(rng, box)
        check_clip(d, rect, f"fuzz-{i}", stats)
        check_arc_length(d, f"fuzz-{i}", stats, rng, n_query=3)
    print(f"随机用例 {n_fuzz} 条: 通过")

    print("== 汇总 ==")
    print(f"裁剪段数: 输入 {stats['segs_in']} -> 输出 {stats['segs_out']}")
    print(f"长度余量 (原长+界-剪长) 最小值: {stats['min_len_margin']:.6g} >= 0")
    print(f"弧长定位查询 {stats['arc_queries']} 次: "
          f"最大实测偏差 {stats['arc_max_dev']:.3g}, "
          f"最大报告误差界 {stats['arc_max_err']:.3g}, "
          f"允许上限 (界+采样间距) {stats['arc_max_bound']:.3g}")
    print("全部断言通过")


def _fmt_box(box) -> str:
    if box is None:
        return "None (空)"
    return "(%.4g, %.4g, %.4g, %.4g)" % box


if __name__ == "__main__":
    main()
