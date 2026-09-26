"""性能基准: 含上千段路径的解析 / 包围盒 / 长度耗时。

用法: python3 scripts/benchmark.py [段数, 默认 5000]
"""

import random
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vecpath import parse, path_bbox, path_length


def make_path(n_seg: int, seed: int = 42) -> str:
    rng = random.Random(seed)
    parts = [f"M {rng.uniform(-100, 100):.4f} {rng.uniform(-100, 100):.4f}"]
    for _ in range(n_seg):
        kind = rng.choices("lcq", weights=[5, 3, 2])[0]
        k = {"l": 2, "c": 6, "q": 4}[kind]
        nums = " ".join(f"{rng.uniform(-20, 20):.4f}" for _ in range(k))
        parts.append(f"{kind} {nums}")  # 相对指令, 同时考验累加
    return " ".join(parts)


def main() -> None:
    n_seg = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    d = make_path(n_seg)
    print(f"路径规模: {n_seg} 段, 数据字符串 {len(d)} 字节")

    t0 = time.perf_counter()
    segs = parse(d)
    t1 = time.perf_counter()
    box = path_bbox(segs)
    t2 = time.perf_counter()

    print(f"解析:   {(t1 - t0) * 1e3:9.3f} ms  ({len(segs)} 段)")
    print(f"包围盒: {(t2 - t1) * 1e3:9.3f} ms  box={tuple(round(v, 4) for v in box)}")
    for tol in (1e-4, 1e-6):
        ta = time.perf_counter()
        length, err = path_length(segs, tol=tol)
        tb = time.perf_counter()
        print(f"长度(tol={tol:.0e}): {(tb - ta) * 1e3:9.3f} ms  "
              f"length={length:.4f} ±{err:.2e}")


if __name__ == "__main__":
    main()
