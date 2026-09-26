"""利用率对比 + 规模耗时基准。运行: python3 benchmark.py"""

import random
import time

from rectpack import pack


def naive_shelf(width, rects):
    """朴素策略：按面积降序排序 + Shelf（逐层水平摆放）。"""
    items = sorted(enumerate(rects), key=lambda t: -(t[1][0] * t[1][1]))
    x = y = shelf_h = 0.0
    unplaced = []
    placed_area = 0.0
    for i, (w, h) in items:
        if w > width:
            unplaced.append(i)
            continue
        if x + w > width:
            y += shelf_h
            x = 0.0
            shelf_h = 0.0
        x += w
        shelf_h = max(shelf_h, h)
        placed_area += w * h
    height = y + shelf_h
    util = placed_area / (width * height) if height > 0 else 0.0
    return height, util, unplaced


def gen_dataset(kind, n, seed):
    rng = random.Random(seed)
    if kind == "uniform":          # 均匀中小矩形
        return [(rng.randint(10, 50), rng.randint(10, 50)) for _ in range(n)]
    if kind == "mixed":            # 少量大板 + 大量小板
        out = [(rng.randint(80, 160), rng.randint(80, 160)) for _ in range(n // 10)]
        out += [(rng.randint(5, 30), rng.randint(5, 30)) for _ in range(n - n // 10)]
        return out
    if kind == "skewed":           # 细长条（旋转收益场景）
        return [(rng.randint(60, 180), rng.randint(4, 12)) for _ in range(n)]
    if kind == "squares":          # 方形为主
        return [(lambda s: (s, s))(rng.randint(8, 60)) for _ in range(n)]
    raise ValueError(kind)


def compare_utilization():
    print("=== 利用率对比：MaxRects(BSSF) vs 朴素Shelf(面积降序) ===")
    print(f"{'数据集':<10}{'n':>5}{'模式':>8}{'朴素高度':>10}{'朴素利用率':>10}"
          f"{'MaxRects高度':>12}{'MaxRects利用率':>13}{'高度降低':>9}")
    width = 200
    for kind in ("uniform", "mixed", "skewed", "squares"):
        for rotate in (False, True):
            hs, us, hs2, us2 = [], [], [], []
            for seed in range(5):
                rects = gen_dataset(kind, 200, seed)
                h0, u0, _ = naive_shelf(width, rects)
                res = pack(width, rects, allow_rotate=rotate)
                assert not res.unplaced
                hs.append(h0); us.append(u0)
                hs2.append(res.height); us2.append(res.utilization)
            h0, u0 = sum(hs) / 5, sum(us) / 5
            h1, u1 = sum(hs2) / 5, sum(us2) / 5
            gain = (h0 - h1) / h0 * 100
            print(f"{kind:<10}{200:>5}{('旋转' if rotate else '不旋转'):>8}"
                  f"{h0:>10.1f}{u0:>10.2%}{h1:>12.1f}{u1:>13.2%}{gain:>8.1f}%")


def scale_timing():
    print("\n=== 规模与耗时（容器宽 400，矩形 5~60，不旋转） ===")
    print(f"{'n':>7}{'耗时(ms)':>12}{'高度':>10}{'利用率':>9}{'ns/矩形':>12}")
    for n in (1000, 2000, 4000, 8000):
        rects = gen_dataset("uniform", n, seed=99)
        t0 = time.perf_counter()
        res = pack(400, rects)
        dt = (time.perf_counter() - t0) * 1000
        assert not res.unplaced
        print(f"{n:>7}{dt:>12.1f}{res.height:>10.0f}{res.utilization:>9.2%}"
              f"{dt * 1e6 / n:>12.0f}")


if __name__ == "__main__":
    compare_utilization()
    scale_timing()
