"""批量判定性能：N 条线段对单个矩形。

复杂度：单条线段判定/裁剪均为 O(1)（端点测试 + 至多 4 条边 / 4 次参数裁剪），
批量 N 条总复杂度 O(N)，无预处理、无分配。
"""
import random
import time

from rect_seg import seg_rect_intersect, clip_segment_to_rect

N = 100_000
RECT = (0.0, 0.0, 10.0, 10.0)


def main():
    rng = random.Random(42)
    segs = [((rng.uniform(-20, 30), rng.uniform(-20, 30)),
             (rng.uniform(-20, 30), rng.uniform(-20, 30)))
            for _ in range(N)]

    t0 = time.perf_counter()
    hits = sum(1 for p, q in segs if seg_rect_intersect(p, q, RECT))
    t1 = time.perf_counter()
    clips = sum(1 for p, q in segs if clip_segment_to_rect(p, q, RECT) is not None)
    t2 = time.perf_counter()

    di, dc = t1 - t0, t2 - t1
    print(f"线段数 N        = {N}")
    print(f"相交判定        : {di*1e3:8.2f} ms  ({di/N*1e9:7.0f} ns/条, {N/di/1e6:.2f} M条/s), 命中 {hits}")
    print(f"相交+裁剪       : {(di+dc)*1e3:8.2f} ms  (裁剪部分 {dc*1e3:.2f} ms), 裁剪非空 {clips}")
    print(f"复杂度          : 单条 O(1)，批量 O(N)")


if __name__ == "__main__":
    main()
