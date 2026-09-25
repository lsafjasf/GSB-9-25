"""多组数据分布下的体积对比：dictcodec vs 逐列朴素存储。

运行：python3 benchmark.py
"""

import random
import string

import dictcodec as dc


def fmt(n):
    return f"{n:,}"


def make_status_like(rng, n):
    """高重复：状态/类型/地区三列，取值极少。"""
    status = ["open", "closed", "pending", "archived", "deleted"]
    types = ["bug", "feature", "task", "epic"]
    regions = ["cn-east", "cn-north", "us-west", "eu-central", "ap-south"]
    return [[rng.choice(status), rng.choice(types), rng.choice(regions)]
            for _ in range(n)]


def make_unique(rng, n):
    """几乎全不同：UUID 风格主键列。"""
    return [[f"7f{i:06x}-{rng.getrandbits(32):08x}-4f3d-a{rng.getrandbits(16):04x}"]
            for i in range(n)]


def make_mixed(rng, n):
    """混合：一列高重复、一列中等基数、一列全不同。"""
    level = ["DEBUG", "INFO", "WARN", "ERROR"]
    return [
        [rng.choice(level),
         f"module-{rng.randint(0, max(1, n // 10))}",
         f"trace-{rng.getrandbits(64):016x}"]
        for _ in range(n)
    ]


def make_single_value(n):
    return [["ACTIVE"] * 1 for _ in range(n)]


def make_empty_strings(rng, n):
    """大量空字符串夹杂少量取值。"""
    pool = ["", "", "", "", "", "timeout", "refused"]
    return [[rng.choice(pool), ""] for _ in range(n)]


def make_long_values(rng, n):
    """超长取值：20 个 2KB 的 JSON 片段循环使用。"""
    alphabet = string.ascii_letters + string.digits
    pool = ["".join(rng.choice(alphabet) for _ in range(2048)) for _ in range(20)]
    return [[rng.choice(pool)] for _ in range(n)]


def make_many_columns(rng, n_rows, n_cols):
    pool = ["a", "b", "c"]
    return [[rng.choice(pool) for _ in range(n_cols)] for _ in range(n_rows)]


def run_case(name, rows):
    blob, stats = dc.encode_with_stats(rows)
    naive = dc.naive_encode(rows)
    assert dc.decode(blob) == rows, f"{name}: 对拍失败"
    ratio = len(blob) / len(naive) if naive else 1.0
    print(f"\n=== {name} ===")
    print(f"rows={stats.rows} cols={stats.cols}  "
          f"naive={fmt(len(naive))}B  dictcodec={fmt(len(blob))}B  "
          f"ratio={ratio:.3f}")
    lines = stats.summary_lines()[:-1]
    if len(lines) > 12:
        for line in lines[:6]:
            print("  " + line)
        print(f"  ... 省略 {len(lines) - 7} 列 ...")
        dict_cols = sum(1 for c in stats.columns if c.mode == "dict")
        print(f"  [汇总] dict 列 {dict_cols} 个，raw 列 {stats.cols - dict_cols} 个")
    else:
        for line in lines:
            print("  " + line)
    return len(blob), len(naive)


def main():
    rng = random.Random(42)
    cases = [
        ("高重复（状态/类型/地区，10万行）", make_status_like(rng, 100_000)),
        ("几乎全不同（UUID 主键，10万行）", make_unique(rng, 100_000)),
        ("混合分布（10万行）", make_mixed(rng, 100_000)),
        ("单值列（10万行）", make_single_value(100_000)),
        ("空字符串为主（10万行）", make_empty_strings(rng, 100_000)),
        ("超长取值 2KB×20 循环（1万行）", make_long_values(rng, 10_000)),
        ("列数极多（2000 列 × 50 行）", make_many_columns(rng, 50, 2000)),
        ("空表（0 行）", []),
    ]
    total_enc = total_naive = 0
    for name, rows in cases:
        enc, naive = run_case(name, rows)
        total_enc += enc
        total_naive += naive
    print(f"\n=== 合计 ===")
    print(f"naive={fmt(total_naive)}B  dictcodec={fmt(total_enc)}B  "
          f"ratio={total_enc / total_naive:.3f}")


if __name__ == "__main__":
    main()
