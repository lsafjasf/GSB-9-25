#!/usr/bin/env python3
"""生成大规模分片覆盖率报告用于性能测试。

用法: python3 tools/gen_benchmark.py [输出目录] [分片数] [文件数] [每文件行数]
默认: bench/ 8 2000 200  (8 个分片 x 2000 文件 x 200 行 ≈ 320 万条 DA 记录)
"""

import random
import sys
import os


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "bench"
    shards = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    files = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
    lines = int(sys.argv[4]) if len(sys.argv) > 4 else 200

    os.makedirs(outdir, exist_ok=True)
    rng = random.Random(42)
    for shard in range(shards):
        path = os.path.join(outdir, f"shard{shard}.info")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"TN:shard{shard}\n")
            for fidx in range(files):
                fh.write(f"SF:src/mod{fidx % 50}/file{fidx}.py\n")
                for lineno in range(1, lines + 1):
                    # 每个分片只覆盖一部分行，模拟真实分片
                    hits = rng.randint(0, 3) if rng.random() < 0.4 else 0
                    fh.write(f"DA:{lineno},{hits}\n")
                fh.write("end_of_record\n")
        size = os.path.getsize(path) / 1024 / 1024
        print(f"{path}: {size:.1f} MiB")


if __name__ == "__main__":
    main()
