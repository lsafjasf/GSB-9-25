#!/usr/bin/env python3
"""
benchmark.py — 分布敏感性数据（仅标准库）。

对多种典型分布，给出四种模式的真实编码体积、相对原始存储的体积变化、
AUTO 选择结果与劣化回退标记，并测量长度 1,000,000 序列的编解码耗时。

原始存储基线：
  fixed64 = 8*n 字节（定长 int64，行业常见朴素基线）
  RAW     = 本库原样模式（ZigZag+LEB128，已随数值分布变化）

运行：
  python3 benchmark.py                 # 控制台表格
  python3 benchmark.py --json out.json # 同时落盘机读数据
"""

import argparse
import json
import platform
import random
import sys
import time

import intcodec as ic

N = 1_000_000


# --------------------------------------------------------------------------
# 分布
# --------------------------------------------------------------------------

def d_timestamps(rng, n):
    """监控时间戳：严格递增、间隔几乎恒定 -> 差分为常数 -> COMBO 极优。"""
    t = 1_700_000_000
    out = []
    for _ in range(n):
        t += rng.choice([10, 10, 10, 10, 11, 20])
        out.append(t)
    return out


def d_smooth_walk(rng, n):
    """平稳小抖动随机游走：差分集中在 [-3,3] -> DELTA 优。"""
    x = rng.randint(-10 ** 6, 10 ** 6)
    out = [x]
    for _ in range(n - 1):
        x += rng.randint(-3, 3)
        out.append(x)
    return out


def d_piecewise_linear(rng, n):
    """分段线性：每段数千点间隔恒定、段间换斜率 -> 差分有长游程 -> COMBO 最优。"""
    out = []
    t = 1_700_000_000
    while len(out) < n:
        gap = rng.choice([10, 25, 60, 100])
        seg = rng.randint(5_000, 20_000)
        for _ in range(seg):
            t += gap
            out.append(t)
    return out[:n]


def d_constant(rng, n):
    """全相同 -> RLE 最优。"""
    return [rng.randint(-100, 100)] * n


def d_runs(rng, n):
    """块状长游程，块值在小范围内变化 -> RLE 优。"""
    out = []
    while len(out) < n:
        v = rng.randint(-50, 50)
        out.extend([v] * rng.randint(50, 500))
    return out[:n]


def d_alternating(rng, n):
    """二值交替：delta 为 +d/-d（COMBO 游程全为 1，必劣化）；RLE 同样不折叠。"""
    a, b = rng.randint(-10 ** 6, 10 ** 6), rng.randint(-10 ** 6, 10 ** 6)
    if b == a:
        b += 1
    return [a if i % 2 == 0 else b for i in range(n)]


def d_all_distinct_small(rng, n):
    """全不同、间隔较大且值在小范围：差分为 ~千级，varint 仍 2 字节。"""
    return [i * rng.choice([97, 113, 211]) + 7 for i in range(n)]


def d_shock(rng, n):
    """剧烈震荡：相邻值在大范围内随机正负跳变 -> 差分与原值同量级，变换全部劣化。"""
    return [rng.randint(-10 ** 9, 10 ** 9) for _ in range(n)]


def d_random64(rng, n):
    """64 位全范围随机：最坏情况，varint 平均 ~9 字节 > 8 字节定长。"""
    return [rng.randint(-2 ** 63, 2 ** 63 - 1) for _ in range(n)]


def d_random_small(rng, n):
    """随机但取值范围小：值仍多为 varint 1~2 字节，RAW 已很小，变换无益。"""
    return [rng.randint(-64, 63) for _ in range(n)]


DISTRIBUTIONS = [
    ("timestamps 时间戳(间隔恒定)", d_timestamps),
    ("smooth_walk 平稳小抖动", d_smooth_walk),
    ("piecewise_linear 分段线性", d_piecewise_linear),
    ("constant 全相同", d_constant),
    ("runs 块状长游程", d_runs),
    ("alternating 二值交替", d_alternating),
    ("distinct_arith 全不同(等差大间隔)", d_all_distinct_small),
    ("shock 剧烈震荡(±1e9)", d_shock),
    ("random_small 随机小值[-64,63]", d_random_small),
    ("random64 64位全范围随机", d_random64),
]


def best_of(fn, times=3):
    best = float("inf")
    for _ in range(times):
        t0 = time.perf_counter()
        r = fn()
        best = min(best, time.perf_counter() - t0)
    return best, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="额外输出机读 JSON")
    ap.add_argument("-n", type=int, default=N, help="序列长度（默认 1,000,000）")
    args = ap.parse_args()
    n = args.n

    rows = []
    print("python %s | n = %d | 帧头 5 字节已计入" % (platform.python_version(), n))
    hdr = ("%-30s %8s %8s %8s %8s %8s %7s %-7s %s"
           % ("分布", "fixed64", "RAW", "DELTA", "RLE", "COMBO",
               "RAW/f64", "AUTO", "劣化?"))
    print(hdr)
    print("-" * len(hdr) + "-" * 22)

    for label, fn in DISTRIBUTIONS:
        seq = fn(random.Random(20260925), n)
        blob, rep = ic.encode_auto(seq)
        s = rep["sizes"]
        fixed = 8 * n
        ratio = s["RAW"] / fixed if fixed else 1.0
        worse = "COMBO劣" if rep["combo_worse_than_raw"] else ""
        print("%-30s %8d %8d %8d %8d %8d %6.3fx %-7s %s"
              % (label, fixed, s["RAW"], s["DELTA"], s["RLE"], s["COMBO"],
                 ratio, rep["winner"], worse))
        rows.append({
            "distribution": label,
            "n": n,
            "fixed64_bytes": fixed,
            "sizes": s,
            "raw_vs_fixed64": round(ratio, 4),
            "auto_winner": rep["winner"],
            "combo_worse_than_raw": rep["combo_worse_than_raw"],
            "features": {
                "runs": rep["runs"],
                "longest_run": rep["longest_run"],
                "distinct": rep["distinct"],
                "max_abs_delta": rep["max_abs_delta"],
                "small_delta_ratio": rep["small_delta_ratio"],
            },
            "choice": rep["choice"],
        })

    # 耗时：代表性的三分布
    print()
    print("百万级耗时（best of 3，含 analyze 选择开销）：")
    print("%-30s %-7s %10s %10s" % ("分布", "AUTO", "编码(s)", "解码(s)"))
    timings = []
    for label, fn in [("timestamps", d_timestamps),
                      ("smooth_walk", d_smooth_walk),
                      ("shock", d_shock),
                      ("random64", d_random64)]:
        seq = fn(random.Random(777), n)

        def enc():
            return ic.encode_auto(seq)
        te, (blob, rep) = best_of(enc)
        td, _ = best_of(lambda: ic.decode(blob))
        assert ic.decode(blob) == seq
        print("%-30s %-7s %10.3f %10.3f" % (label, rep["winner"], te, td))
        timings.append({"distribution": label, "winner": rep["winner"],
                        "encode_s": round(te, 3), "decode_s": round(td, 3)})

    print()
    print("结论速读：")
    print(" * 时间戳（间隔轻微抖动）：差分 1~2 字节，DELTA 胜；COMBO 因每个")
    print("   差分游程还要附计数 varint，反而略大，选择器不会误选。")
    print(" * 分段线性/恒定长间隔：差分出现长游程，COMBO 最优。")
    print(" * 平稳小幅数据：DELTA 显著缩小（小整数 varint 更短）。")
    print(" * 长游程/全相同：RLE 折叠为 (值,计数)。")
    print(" * 交替值：COMBO 游程全为 1，比 RAW 还大 -> 标记劣化并回退 RAW。")
    print(" * 剧烈震荡/64位全随机：差分不减小量级，变换无益甚至变大；")
    print("   且 64 位随机值 varint 平均约 9 字节 > 8 字节定长（见 RAW/f64 列）。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"python": platform.python_version(),
                       "platform": platform.platform(),
                       "n": n, "rows": rows, "timings": timings},
                      f, ensure_ascii=False, indent=2)
        print("\n机读数据已写入 %s" % args.json)


if __name__ == "__main__":
    main()
