#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规模基准: 生成 1 万个文件(默认合计 2 GiB), 测量扫描耗时与内存峰值。

用法:
    python3 benchmark.py [--files 10000] [--total-gib 2.0] [--dir bench_data]
                         [--keep] [--skip-bigfile]
"""

import argparse
import os
import random
import resource
import shutil
import sys
import time

from chunkdedup import CDCChunker, scan_file, scan_paths


def rss_mib() -> float:
    """当前进程峰值 RSS (Linux: ru_maxrss 单位 KiB)。"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def gen_dataset(root: str, num_files: int, total_bytes: int,
                seed: int = 7) -> None:
    """50% 随机独立文件 + 25% 完全重复组 + 25% 近似(插入少量内容)文件。"""
    rng = random.Random(seed)
    os.makedirs(root, exist_ok=True)
    mean_size = total_bytes / num_files

    def rsize() -> int:
        return max(512, int(rng.lognormvariate(
            __import__("math").log(mean_size), 0.6)))

    plan = []  # (kind, payload_or_none)
    budget = total_bytes
    n_unique = num_files // 2
    n_exact = num_files // 4
    n_near = num_files - n_unique - n_exact

    for _ in range(n_unique):
        plan.append(("unique", rsize()))
    i = 0
    while i < n_exact:
        copies = rng.randint(2, 4)
        size = rsize()
        for _ in range(min(copies, n_exact - i)):
            plan.append(("exact", size))
        i += copies
    i = 0
    while i < n_near:
        members = rng.randint(2, 4)
        size = rsize()
        for k in range(min(members, n_near - i)):
            plan.append(("near_base" if k == 0 else "near_edit", size))
        i += members
    rng.shuffle(plan)

    exact_pool = {}
    last_base = None
    written = 0
    t0 = time.perf_counter()
    for idx, (kind, size) in enumerate(plan):
        path = os.path.join(root, f"f{idx:05d}.bin")
        if kind == "unique":
            payload = os.urandom(size)
        elif kind == "exact":
            payload = exact_pool.get(size)
            if payload is None:
                payload = os.urandom(size)
                exact_pool[size] = payload
        elif kind == "near_base" or (kind == "near_edit" and last_base is None):
            payload = os.urandom(size)
            last_base = payload
        else:  # near_edit: 取最近一个 base, 随机插入 1~3 段
            base = last_base
            payload = bytearray(base)
            for _ in range(rng.randint(1, 3)):
                pos = rng.randrange(len(payload) + 1)
                payload[pos:pos] = os.urandom(rng.randint(1024, 16384))
            payload = bytes(payload)
        with open(path, "wb") as f:
            f.write(payload)
        written += len(payload)
        if (idx + 1) % 2000 == 0:
            print(f"  生成 {idx + 1}/{len(plan)} ...", file=sys.stderr)
    dt = time.perf_counter() - t0
    print(f"数据集: {len(plan)} 个文件, {human(written)}, "
          f"生成耗时 {dt:.1f}s", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=10000)
    ap.add_argument("--total-gib", type=float, default=2.0)
    ap.add_argument("--dir", default="bench_data")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--skip-gen", action="store_true")
    ap.add_argument("--skip-bigfile", action="store_true")
    args = ap.parse_args()

    total = int(args.total_gib * (1 << 30))
    if not args.skip_gen:
        shutil.rmtree(args.dir, ignore_errors=True)
        gen_dataset(args.dir, args.files, total)

    # ---- 批量扫描 ----
    rss0 = rss_mib()
    t0 = time.perf_counter()
    index = scan_paths([args.dir], progress=True)
    dt = time.perf_counter() - t0
    rss1 = rss_mib()

    total_bytes = sum(r.size for r in index.files)
    exact = index.exact_groups()
    groups = index.similar_groups(0.3)
    gib = total_bytes / (1 << 30)
    print("\n===== 批量扫描结果 =====")
    print(f"文件数:        {len(index.files)}")
    print(f"总大小:        {human(total_bytes)} ({gib:.2f} GiB)")
    print(f"耗时:          {dt:.1f}s  ({total_bytes/2**20/dt:.1f} MiB/s, "
          f"{len(index.files)/dt:.0f} 文件/s)")
    print(f"块总数:        {index.num_chunks()}")
    print(f"不同块数:      {index.num_unique_chunks()} "
          f"(去重率 {1 - index.num_unique_chunks()/max(1, index.num_chunks()):.1%})")
    print(f"完全重复组:    {len(exact)} 组")
    print(f"相似组:        {len(groups)} 组")
    print(f"进程峰值 RSS:  {rss1:.0f} MiB (扫描新增约 {rss1-rss0:.0f} MiB)")
    per_gib = dt / gib
    print(f"按实测吞吐外推: 5 GiB 约 {per_gib*5/60:.1f} 分钟, "
          f"10 GiB 约 {per_gib*10/60:.1f} 分钟")

    # ---- 单个大文件的流式内存 ----
    if not args.skip_bigfile:
        big = os.path.join(args.dir, "bigfile_1gib.bin")
        gib1 = 1 << 30
        print(f"\n===== 单文件流式测试 ({human(gib1)}) =====")
        with open(big, "wb") as f:
            chunk = os.urandom(1 << 20)
            for _ in range(gib1 // (1 << 20)):
                f.write(chunk)
        rss_before = rss_mib()
        t0 = time.perf_counter()
        counter = [0]
        rec = scan_file(big, chunk_sink=lambda c: counter.__setitem__(
            0, counter[0] + 1))
        dt = time.perf_counter() - t0
        rss_after = rss_mib()
        print(f"大小:          {human(rec.size)}")
        print(f"耗时:          {dt:.1f}s ({rec.size/2**20/dt:.1f} MiB/s)")
        print(f"块数:          {counter[0]}")
        print(f"扫描前后峰值 RSS: {rss_before:.0f} -> {rss_after:.0f} MiB "
              f"(增量 {rss_after-rss_before:.0f} MiB, 与文件大小无关)")

    if not args.keep:
        shutil.rmtree(args.dir, ignore_errors=True)
        print(f"\n已清理 {args.dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
