"""采样检验: 验证包围盒不低估真实曲线范围。

对每条测试路径的每个曲线段, 在 t ∈ [0,1] 上密集采样,
断言所有采样点都落在包围盒内 (允许 1e-9 的浮点余量)。
包含固定用例与随机模糊测试 (固定种子, 可复现)。

用法: python3 scripts/sample_check.py
"""

import random
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vecpath import parse, point_at, path_bbox

SAMPLES_PER_SEGMENT = 2000
EPS = 1e-9

FIXED_PATHS = [
    "M 0 0 L 10 0 L 10 10 L 0 10 Z",
    "M 0 0 Q 1 2 2 0 T 4 0 T 6 0",
    "M 0 0 C 0 3 1 -3 1 0 S 2 -3 2 0",
    "M 0 0 C 5 10 15 -10 20 0 Q 25 10 30 0 C 35 -10 40 5 45 0 Z",
    "m 3 4 c 1 2 3 -4 5 0 q 2 6 4 0 l -1 -1 z",
    "M 1 1 L 1 1 C 1 1 1 1 1 1 Q 1 1 1 1 Z",  # 全退化
    "M -1e3 2e2 C 3e2 -4e2 5e2 6e2 -7e2 -8e2",  # 大坐标
]


def random_path(rng: random.Random, n_seg: int) -> str:
    """生成随机相对坐标路径 (同时检验相对累加与包围盒)。"""
    parts = [f"m {rng.uniform(-50, 50)} {rng.uniform(-50, 50)}"]
    for _ in range(n_seg):
        kind = rng.choice("lcq")
        nums = " ".join(f"{rng.uniform(-10, 10):.6f}" for _ in range({"l": 2, "c": 6, "q": 4}[kind]))
        parts.append(f"{kind} {nums}")
    if rng.random() < 0.5:
        parts.append("z")
    return " ".join(parts)


def check_path(d: str, label: str) -> int:
    """返回采样点总数; 若有采样点落在盒外则抛 AssertionError。"""
    segs = parse(d)
    box = path_bbox(segs)
    n_pts = 0
    for seg in segs:
        if seg.kind == "M":
            continue
        for i in range(SAMPLES_PER_SEGMENT + 1):
            x, y = point_at(seg, i / SAMPLES_PER_SEGMENT)
            n_pts += 1
            assert box is not None
            assert box[0] - EPS <= x <= box[2] + EPS, (
                f"[{label}] 采样点越界 x={x} 盒={box} 段={seg}")
            assert box[1] - EPS <= y <= box[3] + EPS, (
                f"[{label}] 采样点越界 y={y} 盒={box} 段={seg}")
    return n_pts


def main() -> None:
    total_pts = 0
    for idx, d in enumerate(FIXED_PATHS):
        total_pts += check_path(d, f"fixed-{idx}")
    print(f"固定用例 {len(FIXED_PATHS)} 条: 通过")

    rng = random.Random(20260926)
    n_fuzz = 200
    for i in range(n_fuzz):
        total_pts += check_path(random_path(rng, rng.randint(1, 30)), f"fuzz-{i}")
    print(f"随机用例 {n_fuzz} 条: 通过")
    print(f"共检验采样点 {total_pts} 个, 无一落在包围盒外")


if __name__ == "__main__":
    main()
