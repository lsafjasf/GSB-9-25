"""benchmark —— 利用率对比与规模耗时。运行：python3 benchmark.py"""

import random
import time

from rectpack import pack, pack_naive


def gen_uniform(n, seed, lo, hi):
    rng = random.Random(seed)
    return [(rng.randint(lo, hi), rng.randint(lo, hi)) for _ in range(n)]


def gen_mixed(n, seed):
    """少量大面板 + 大量小面板，模拟导出版面。"""
    rng = random.Random(seed)
    rects = []
    for i in range(n):
        if i % 20 == 0:
            rects.append((rng.randint(60, 120), rng.randint(40, 90)))
        else:
            rects.append((rng.randint(5, 30), rng.randint(5, 30)))
    return rects


def gen_stripes(n, seed):
    """细长条为主（旋转模式收益大的场景）。"""
    rng = random.Random(seed)
    return [(rng.randint(40, 90), rng.randint(4, 12)) for _ in range(n)]


def compare(name, width, rects, allow_rotation):
    t0 = time.perf_counter()
    fast = pack(width, rects, allow_rotation=allow_rotation)
    t1 = time.perf_counter()
    naive = pack_naive(width, rects, allow_rotation=allow_rotation)
    t2 = time.perf_counter()
    gain_h = (naive.height - fast.height) / naive.height * 100
    print(f"{name:<28} n={len(rects):<5} 旋转={'是' if allow_rotation else '否'}  "
          f"MaxRects: 高={fast.height:<7} 利用率={fast.utilization:.2%}  "
          f"朴素: 高={naive.height:<7} 利用率={naive.utilization:.2%}  "
          f"高度降低={gain_h:5.1f}%  耗时={t1 - t0:.3f}s/{t2 - t1:.3f}s"
          f"  未放置={len(fast.unplaced)}")


def scale(n, seed):
    rects = gen_mixed(n, seed)
    t0 = time.perf_counter()
    res = pack(400, rects, allow_rotation=True)
    dt = time.perf_counter() - t0
    print(f"n={n:<6} 耗时={dt:7.3f}s  高度={res.height:<8} "
          f"利用率={res.utilization:.2%}  平均每矩形={dt / n * 1e6:8.1f}µs")


if __name__ == "__main__":
    print("=== 利用率对比（MaxRects vs 按面积排序的朴素 Shelf） ===")
    compare("均匀小矩形", 100, gen_uniform(300, 1, 5, 30), False)
    compare("均匀小矩形", 100, gen_uniform(300, 1, 5, 30), True)
    compare("大小混合", 200, gen_mixed(400, 2), False)
    compare("大小混合", 200, gen_mixed(400, 2), True)
    compare("细长条", 150, gen_stripes(300, 3), False)
    compare("细长条", 150, gen_stripes(300, 3), True)
    compare("均匀中方块", 300, gen_uniform(800, 4, 10, 45), True)

    print("\n=== 规模与耗时（宽 400，混合数据，允许旋转） ===")
    for n in (500, 1000, 2000, 5000):
        scale(n, seed=10)
