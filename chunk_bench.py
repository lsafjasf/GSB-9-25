#!/usr/bin/env python3
"""
chunk_bench.py — 分块数对压缩率的影响（仅标准库）。

对三类典型数据（递增时间戳 / 小波动游走 / 剧烈震荡），按固定块大小扫描
分块数（1 .. 65536），另附自适应切分对照；每档输出：
  分块数、块表开销、总字节、相对整段最优(单块)的膨胀比、相对 fixed64 的压缩率。
每个配置都做「分块解码 == 整段 AUTO 解码 == 原序列」对拍后才计入表格。

运行：
  python3 chunk_bench.py                 # 控制台表格
  python3 chunk_bench.py --json out.json # 同时落盘机读数据
"""

import argparse
import json
import platform
import random
import time

import intcodec as ic

N = 1_000_000
SEED = 20260929


def d_timestamps(rng, n):
    """递增时间戳：间隔几乎恒定。"""
    t = 1_700_000_000
    out = []
    for _ in range(n):
        t += rng.choice([10, 10, 10, 10, 11, 20])
        out.append(t)
    return out


def d_smooth_walk(rng, n):
    """小波动：差分 ∈ [-3,3] 的随机游走。"""
    x = rng.randint(-10 ** 6, 10 ** 6)
    out = [x]
    for _ in range(n - 1):
        x += rng.randint(-3, 3)
        out.append(x)
    return out


def d_shock(rng, n):
    """剧烈震荡：±1e9 随机跳变。"""
    return [rng.randint(-10 ** 9, 10 ** 9) for _ in range(n)]


DISTRIBUTIONS = [
    ("timestamps 递增时间戳", d_timestamps),
    ("smooth_walk 小波动游走", d_smooth_walk),
    ("shock 剧烈震荡(±1e9)", d_shock),
]

# 目标分块数档位（n=1e6 时对应块大小 1e6..16，最后一档块大小 16）
CHUNK_COUNTS = [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="额外输出机读 JSON")
    ap.add_argument("-n", type=int, default=N, help="序列长度（默认 1,000,000）")
    args = ap.parse_args()
    n = args.n

    all_rows = []
    print("python %s | n = %d | 每档均已对拍：分块解码 == 整段 AUTO 解码 == 原序列"
          % (platform.python_version(), n))

    for label, fn in DISTRIBUTIONS:
        seq = fn(random.Random(SEED), n)
        fixed64 = 8 * n
        whole_blob, whole_rep = ic.encode_auto(seq)
        whole = ic.decode(whole_blob)
        assert whole == seq
        base = len(whole_blob)

        print()
        print("== %s ==  整段最优 AUTO=%s %d B（fixed64 基线 %d B）"
              % (label, whole_rep["winner"], base, fixed64))
        hdr = "%9s %9s %12s %10s %12s %10s %8s  %s" % (
            "分块数", "块大小", "块表开销(B)", "总字节", "vs 整段最优",
            "vs fixed64", "耗时(ms)", "块内模式分布")
        print(hdr)
        print("-" * (len(hdr) + 8))

        def emit(chunks, chunk_size, rep, elapsed_ms, tag):
            total = rep["total_bytes"]
            modes = " ".join("%s:%d" % (k, v) for k, v in
                             rep["chunk_modes"].items() if v)
            print("%9s %9s %12d %10d %11.4fx %9.4fx %8.1f  %s"
                  % (tag, chunk_size, rep["table_bytes"], total,
                     total / base, total / fixed64, elapsed_ms, modes))
            all_rows.append({
                "distribution": label, "chunks": chunks,
                "chunk_size": chunk_size, "table_bytes": rep["table_bytes"],
                "total_bytes": total, "vs_whole_best": round(total / base, 4),
                "vs_fixed64": round(total / fixed64, 4),
                "encode_ms": round(elapsed_ms, 1),
                "chunk_modes": {k: v for k, v in rep["chunk_modes"].items() if v},
            })

        for k in CHUNK_COUNTS:
            if k > n:
                continue
            chunk_size = (n + k - 1) // k
            t0 = time.perf_counter()
            blob, rep = ic.encode_chunked(seq, chunk_size=chunk_size)
            el = (time.perf_counter() - t0) * 1000
            assert ic.decode(blob) == whole, "分块/整段对拍失败 k=%d" % k
            emit(rep["chunks"], chunk_size, rep, el, str(k))

        # 自适应切分对照（基础块 1024）
        t0 = time.perf_counter()
        blob, rep = ic.encode_chunked(seq, adaptive=True, block_size=1024)
        el = (time.perf_counter() - t0) * 1000
        assert ic.decode(blob) == whole, "自适应分块对拍失败"
        emit(rep["chunks"], "自适应", rep, el, "adaptive")

    print()
    print("结论速读：")
    print(" * 分块数=1 时 CHUNKED 帧仅比整段帧多块表几个字节，模式选择一致。")
    print(" * 分布均匀的数据（本表三类）分块收益≈0，块表开销随块数线性上涨；")
    print("   每块表项约 3~5 B（模式1B + 元素数/长度 varint），65536 块时")
    print("   块表达数十万字节，压缩率明显变差 —— 块数需按分布变化点选择。")
    print(" * 分块的真正价值在分布突变的混合数据：每块各自选最优模式，")
    print("   自适应切分在分布边界处断块，兼顾块表开销与逐块最优化。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"python": platform.python_version(), "n": n,
                       "seed": SEED, "rows": all_rows},
                      f, ensure_ascii=False, indent=2)
        print("\n机读数据已写入 %s" % args.json)


if __name__ == "__main__":
    main()
