"""分布敏感性基准：不同数据分布下各模式的体积对比 + 百万级耗时。

运行：python3 bench.py
"""
import random
import time

import codec
from codec import Mode

N = 200_000
rng = random.Random(2026)


def gen_monotone_ts(n):          # 监控时间戳：递增、步长小
    t, out = 1_700_000_000_000, []
    for _ in range(n):
        t += rng.randint(1, 9)
        out.append(t)
    return out


def gen_slow_walk(n):            # 数值变化很小的指标（随机游走 ±2）
    v, out = 1000, []
    for _ in range(n):
        v += rng.randint(-2, 2)
        out.append(v)
    return out


def gen_constant(n):             # 全相同
    return [42] * n


def gen_long_runs(n):            # 长游程：状态量，偶尔切换
    out = []
    while len(out) < n:
        out.extend([rng.randint(0, 3)] * rng.randint(50, 500))
    return out[:n]


def gen_random_uniform(n):       # 均匀随机大数（压缩天敌）
    return [rng.randint(-(2**31), 2**31 - 1) for _ in range(n)]


def gen_oscillating(n):          # 剧烈震荡：正负交替大幅跳变
    return [(-1) ** i * (2**40 + rng.randint(0, 10**6)) for i in range(n)]


def gen_all_distinct(n):         # 全不同递增大数
    base = 2**63
    return [base + i * 10**6 for i in range(n)]


CASES = [
    ("递增时间戳(步长1-9)", gen_monotone_ts),
    ("小幅随机游走(±2)", gen_slow_walk),
    ("全相同常量", gen_constant),
    ("长游程状态量", gen_long_runs),
    ("均匀随机±2^31", gen_random_uniform),
    ("剧烈震荡±2^40", gen_oscillating),
    ("全不同递增大数", gen_all_distinct),
]


def main():
    print(f"样本量 n={N}，基线=每元素 8 字节定长（int64）\n")
    header = f"{'分布':<20}{'基线':>10}{'RAW':>10}{'DELTA':>10}{'RLE':>10}{'D+RLE':>10}{'自动选择':>11}{'压缩率':>9}"
    print(header)
    print("-" * len(header))
    for name, gen in CASES:
        seq = gen(N)
        res = codec.encode_with_report(seq)
        base = N * 8
        row = f"{name:<20}{base:>10}"
        for m in ("raw", "delta", "rle", "delta_rle"):
            row += f"{res.sizes[m]:>10}"
        ratio = res.size / base
        row += f"{res.mode.name:>11}{ratio:>8.1%}"
        print(row)

    print("\n说明：压缩率 = 自动选择后字节数 / 基线；>100% 即劣化（本库自动回退 RAW 后上限约 100%）。")

    # 百万级耗时
    n = 1_000_000
    print(f"\n百万级耗时（n={n}）：")
    for name, gen in (("递增时间戳", gen_monotone_ts), ("均匀随机±2^31", gen_random_uniform)):
        seq = gen(n)
        t0 = time.perf_counter()
        res = codec.encode_with_report(seq)
        t1 = time.perf_counter()
        out = codec.decode(res.data)
        t2 = time.perf_counter()
        assert out == seq
        print(f"  {name}: mode={res.mode.name}, {n * 8}B -> {res.size}B "
              f"({res.size / (n * 8):.1%}), encode(auto) {t1 - t0:.2f}s, decode {t2 - t1:.2f}s")


if __name__ == "__main__":
    main()
