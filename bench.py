"""批量判定性能：N 条线段对单个矩形。

每条线段的判定为固定次数（<= 2 次点-矩形 + <= 4 次线段距离，每次 O(1)），
裁剪为 Liang-Barsky 4 对边界的固定计算，因此单条 O(1)，批量 O(N)。

用法: python3 bench.py [N，默认 100000]
"""

import random
import sys
import time

import rectclip as rc

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
RECT = (0.0, 0.0, 100.0, 50.0)


def main():
    rng = random.Random(7)
    segs = []
    for _ in range(N):
        x1 = rng.uniform(-120, 220)
        y1 = rng.uniform(-70, 120)
        x2 = x1 + rng.uniform(-80, 80)
        y2 = y1 + rng.uniform(-40, 40)
        segs.append(((x1, y1), (x2, y2)))

    # 相交判定
    t0 = time.perf_counter()
    hits = 0
    for p1, p2 in segs:
        hits += rc.segment_rect_intersects(p1, p2, RECT)
    t1 = time.perf_counter()
    dt_hit = t1 - t0

    # 裁剪
    t0 = time.perf_counter()
    clipped = 0
    for p1, p2 in segs:
        if rc.clip_segment_to_rect(p1, p2, RECT) is not None:
            clipped += 1
    t1 = time.perf_counter()
    dt_clip = t1 - t0

    print(f"线段数 N = {N:,}，矩形 = {RECT}，相交 {hits:,} 条，裁剪非空 {clipped:,} 条")
    print(f"segment_rect_intersects : {dt_hit*1e3:9.2f} ms  "
          f"({dt_hit/N*1e9:7.1f} ns/条,  {N/dt_hit:,.0f} 条/秒)")
    print(f"clip_segment_to_rect    : {dt_clip*1e3:9.2f} ms  "
          f"({dt_clip/N*1e9:7.1f} ns/条,  {N/dt_clip:,.0f} 条/秒)")


if __name__ == "__main__":
    main()
