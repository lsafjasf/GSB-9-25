"""性能测试：含上千段路径的解析、包围盒、长度计算耗时。

运行：python3 perf.py
"""

import random
import time

import pathgeom
from pathgeom import parse_path, path_bbox, path_length, path_length_error_bound


def make_path(rng, n_segs):
    """生成含 n_segs 个绘制段的路径字符串（L/Q/C 混合，绝对/相对混合）。"""
    parts = ["M %.4f %.4f" % (rng.uniform(-1000, 1000), rng.uniform(-1000, 1000))]
    for _ in range(n_segs):
        r = rng.random()
        if r < 0.34:
            parts.append("l %.4f %.4f" % (rng.uniform(-50, 50), rng.uniform(-50, 50)))
        elif r < 0.67:
            parts.append("Q %.4f %.4f %.4f %.4f" %
                         tuple(rng.uniform(-100, 100) for _ in range(4)))
        else:
            parts.append("c %.4f %.4f %.4f %.4f %.4f %.4f" %
                         tuple(rng.uniform(-50, 50) for _ in range(6)))
    parts.append("Z")
    return " ".join(parts)


def bench(n_segs, length_n=64, repeat=3):
    rng = random.Random(12345)
    d = make_path(rng, n_segs)
    best = {}
    for _ in range(repeat):
        t0 = time.perf_counter()
        segs = parse_path(d)
        t1 = time.perf_counter()
        box = path_bbox(segs)
        t2 = time.perf_counter()
        length = path_length(segs, length_n)
        bound = path_length_error_bound(segs, length_n)
        t3 = time.perf_counter()
        best["parse"] = min(best.get("parse", 1e9), t1 - t0)
        best["bbox"] = min(best.get("bbox", 1e9), t2 - t1)
        best["length"] = min(best.get("length", 1e9), t3 - t2)
    return d, segs, box, length, bound, best


def main():
    print("Python %s" % sys_version())
    print("长度细分数 n=64，每规模取 3 次运行最小值")
    print("%-8s %10s %12s %12s %12s %12s" %
          ("段数", "字符数", "解析(ms)", "包围盒(ms)", "长度(ms)", "总长(含界)"))
    for n_segs in (1000, 5000, 20000, 50000):
        d, segs, box, length, bound, t = bench(n_segs)
        print("%-8d %10d %12.2f %12.2f %12.2f %12.2f" %
              (len(segs) - 1, len(d), t["parse"] * 1e3,
               t["bbox"] * 1e3, t["length"] * 1e3,
               (t["parse"] + t["bbox"] + t["length"]) * 1e3))
        print("         bbox=%s length=%.4f 误差上界=%.4e"
              % (tuple(round(v, 2) for v in box), length, bound))


def sys_version():
    import sys
    return sys.version.split()[0]


if __name__ == "__main__":
    main()
