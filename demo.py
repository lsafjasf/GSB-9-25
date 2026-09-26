"""demo.py: 按不同数据分布生成数据集，输出逐列体积对比表。"""

import random
import string
import uuid

from dictcodec import encode_with_stats, decode


def make_datasets(seed: int = 42) -> dict[str, list[list[str]]]:
    rng = random.Random(seed)
    n = 5000

    statuses = ["ACTIVE", "PENDING", "CLOSED", "SUSPENDED", "ARCHIVED"]
    regions = [f"region-{i:03d}" for i in range(50)]
    types = ["A", "B", "C"]

    # 1. 高重复度：状态/类型/地区类列
    repetitive = [[
        rng.choice(statuses),
        rng.choice(types),
        rng.choice(regions),
    ] for _ in range(n)]

    # 2. 几乎全不相同：UUID 类列（应触发 raw 回退）
    unique = [[str(uuid.uuid4()), rng.choice(statuses)] for _ in range(n)]

    # 3. 混合：唯一 ID + 高重复状态 + 单值常量列 + 大量空字符串
    mixed = [[
        str(i),                          # 全不相同 -> raw
        rng.choice(statuses),            # 高重复 -> dict
        "CONSTANT",                      # 单值 -> dict（字典仅 1 项）
        "" if rng.random() < 0.9 else "x",  # 90% 空字符串 -> dict
    ] for i in range(n)]

    # 4. 长尾分布：少量热值 + 大量只出现一次的值
    hot = ["hot"] * (n // 2)
    tail = [f"tail-{i}" for i in range(n - len(hot))]
    rng.shuffle(tail)
    longtail = [[v] for v in hot + tail]

    # 5. 短整数字符串：低基数但值很短
    nums = [[str(rng.randrange(10))] for _ in range(n)]

    return {
        "repetitive(状态/类型/地区)": repetitive,
        "unique(UUID 全不同)": unique,
        "mixed(ID/状态/常量/空串)": mixed,
        "longtail(半热值半唯一)": longtail,
        "lowcard-short(0-9 数字串)": nums,
    }


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB"):
        if n < 1024 or unit == "MiB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}B"


def main() -> None:
    grand_raw = grand_enc = 0
    for name, records in make_datasets().items():
        blob, stats = encode_with_stats(records)
        # 对拍：解码必须逐格一致
        assert decode(blob) == records, f"{name}: 对拍失败"

        print(f"\n=== {name}  ({stats.num_rows} 行 x {stats.num_cols} 列) ===")
        print(f"{'列':>3} {'模式':<5} {'字典大小':>8} {'编码前':>12} "
              f"{'编码后':>12} {'压缩比':>8}")
        for c in stats.columns:
            print(f"{c.index:>3} {c.mode:<5} {c.dict_size:>8} "
                  f"{human(c.raw_bytes):>12} {human(c.encoded_bytes):>12} "
                  f"{c.ratio:>7.1%}")
        print(f"    合计: {human(stats.total_raw)} -> {human(stats.total_encoded)}"
              f"  (整体 {stats.total_encoded / max(stats.total_raw, 1):.1%},"
              f" 含头部共 {len(blob)} 字节)")
        grand_raw += stats.total_raw
        grand_enc += stats.total_encoded

    print(f"\n全部数据集合计: {human(grand_raw)} -> {human(grand_enc)}"
          f"  ({grand_enc / grand_raw:.1%})")


if __name__ == "__main__":
    main()
