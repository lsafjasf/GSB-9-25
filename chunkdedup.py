#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chunkdedup -- 文件指纹与内容定义分块(CDC)重复检测库。仅依赖 Python 3 标准库。

核心能力
--------
1. 整文件 SHA-256 指纹: 识别字节级完全重复文件(含空文件)。
2. Gear 滚动哈希内容定义分块: 按滑动窗口的边界条件切块, 在文件中插入/删除
   少量内容后, 只有改动点附近 1~2 个块的边界发生变化, 其余块指纹完全一致。
3. 流式处理: 读块 -> 滚动哈希 -> 产出已切分块 -> 逐块哈希,
   单次驻留内存上界为 max_size + read_size, 与文件大小无关。
4. 批量索引: 完全重复组、相似组(Dice 相似度 / 共享块比例 / 共享字节比例)、
   单文件内重复块。

用法
----
    python3 chunkdedup.py scan 路径... [--threshold 0.3] [--json out.json]
    python3 chunkdedup.py demo            # 生成样例数据并输出检测报告
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

MASK64 = (1 << 64) - 1
DIGEST_SIZE = 16  # blake2b-128


def _gear_table(seed: int = 0x9E3779B97F4A7C15) -> Tuple[int, ...]:
    """固定种子生成 256 项 64 位随机表(确定性、跨进程一致)。"""
    rng = random.Random(seed)
    return tuple(rng.getrandbits(64) for _ in range(256))


DEFAULT_GEAR_TABLE = _gear_table()


@dataclass(frozen=True)
class Chunk:
    """一个内容定义块(offset/length 是该块在所属文件中的位置)。"""
    offset: int
    length: int
    digest: bytes  # blake2b-128 摘要


class CDCChunker:
    """Gear-hash 内容定义分块器(流式)。

    边界规则
    ~~~~~~~~
    对最近 window 个字节维护滚动 Gear 哈希:

        fp = ((fp << 1) mod 2^64) + GEAR[byte]

    当 fp 的低 log2(avg_size) 位全为 0 时定义为内容边界。每个字节成为边界的
    概率约 1/avg_size, 块长近似几何分布, 期望长度 avg_size。辅以:
      * min_size: 距上一边界不足 min_size 不切分(消除大量小块);
      * max_size: 超过 max_size 强制切分(保证最坏块长有界)。

    不变量: update() 可被任意大小分块调用(例如每次 1 MiB), 对同一字节流
    产出的块序列与一次性喂入完全相同。
    内存驻留 <= max_size + 单次喂入长度。
    """

    def __init__(
        self,
        min_size: int = 2 * 1024,
        avg_size: int = 8 * 1024,
        max_size: int = 64 * 1024,
        window: int = 48,
        table: Optional[Tuple[int, ...]] = None,
    ) -> None:
        if avg_size & (avg_size - 1) != 0:
            raise ValueError("avg_size 必须是 2 的幂")
        if not (window <= min_size <= avg_size <= max_size):
            raise ValueError("需要满足 window <= min_size <= avg_size <= max_size")
        self.min_size = min_size
        self.avg_size = avg_size
        self.max_size = max_size
        self.window = window
        self.mask = avg_size - 1
        self._table = table if table is not None else DEFAULT_GEAR_TABLE
        self._buf = bytearray()   # 自上一切分点起尚未切分的数据
        self._base = 0            # _buf[0] 在文件中的绝对偏移
        self._pos = min_size - 1  # 下一个待判定边界的下标(相对 _buf)

    def update(self, data: bytes) -> List[Chunk]:
        if data:
            self._buf.extend(data)
        return self._scan()

    def finish(self) -> Optional[Chunk]:
        """刷出不足一个边界的尾部数据; 空文件返回 None。"""
        if not self._buf:
            return None
        chunk = self._emit(0, len(self._buf))
        self._buf.clear()
        return chunk

    def _emit(self, rel: int, end: int) -> Chunk:
        body = bytes(self._buf[rel:end])
        digest = hashlib.blake2b(body, digest_size=DIGEST_SIZE).digest()
        return Chunk(self._base + rel, len(body), digest)

    def _scan(self) -> List[Chunk]:
        chunks: List[Chunk] = []
        buf = self._buf
        tbl = self._table
        mask = self.mask
        mask64 = MASK64
        window = self.window
        lo = self.min_size - 1   # 允许检查边界的最小下标
        hi = self.max_size - 1   # 块内偏移到此强制切分
        n = len(buf)
        start = 0                # 当前块在 buf 中的起点
        p = self._pos
        if p < lo:
            p = lo
        while p < n:
            # fp 只需覆盖以 p 结尾的最近 window 个字节; min_size >= window
            # 保证 p - window + 1 >= start。min_size 之前的字节被跳过, 不参与
            # 哈希(它们不可能影响任何边界判定), 这是标准 CDC 加速手段。
            fp = 0
            for b in buf[p - window + 1:p + 1]:
                fp = ((fp << 1) & mask64) + tbl[b]
            stop = start + hi
            if stop > n - 1:
                stop = n - 1
            cut = -1
            while True:
                if (fp & mask) == 0:
                    cut = p
                    break
                if p >= stop:
                    break
                p += 1
                fp = ((fp << 1) & mask64) + tbl[buf[p]]
            if cut < 0:
                if p >= start + hi:
                    cut = p          # 达到 max_size, 强制切分
                else:
                    p += 1           # 数据耗尽, 位置 n-1 已检查过
                    continue
            end = cut + 1
            chunks.append(self._emit(start, end))
            start = end
            p = start + lo
        # 已切分的前缀只在每次 _scan 末尾压缩一次, 避免逐块 memmove。
        if start:
            del buf[:start]
            self._base += start
            p -= start
        self._pos = p
        return chunks


# ----------------------------------------------------------------------
# 文件级扫描
# ----------------------------------------------------------------------
@dataclass
class FileRecord:
    file_id: int
    path: str
    size: int
    sha256: str
    chunks: List[Chunk] = field(default_factory=list)

    def chunk_counts(self) -> Dict[bytes, int]:
        counts: Dict[bytes, int] = {}
        for ch in self.chunks:
            counts[ch.digest] = counts.get(ch.digest, 0) + 1
        return counts


def scan_file(
    path: str,
    chunker: Optional[CDCChunker] = None,
    read_size: int = 1 << 20,
    chunk_sink: Optional[Callable[[Chunk], None]] = None,
) -> FileRecord:
    """流式扫描单个文件: SHA-256 + CDC 分块。

    chunk_sink 非 None 时, 块只回调给 sink、不保留在 FileRecord.chunks 中,
    此时扫描阶段额外内存恒为 O(max_size + read_size), 与文件大小无关。
    """
    chunker = chunker or CDCChunker()
    h = hashlib.sha256()
    saved: Optional[List[Chunk]] = [] if chunk_sink is None else None
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(read_size)
            if not block:
                break
            size += len(block)
            h.update(block)
            produced = chunker.update(block)
            if chunk_sink is None:
                saved.extend(produced)
            else:
                for ch in produced:
                    chunk_sink(ch)
    tail = chunker.finish()
    if tail is not None:
        if chunk_sink is None:
            saved.append(tail)
        else:
            chunk_sink(tail)
    return FileRecord(-1, path, size, h.hexdigest(), saved if saved else [])


# ----------------------------------------------------------------------
# 批量索引与重复/相似检测
# ----------------------------------------------------------------------
@dataclass
class PairSimilarity:
    a: int
    b: int
    shared_chunks: int
    shared_bytes: int
    chunks_a: int
    chunks_b: int
    bytes_a: int
    bytes_b: int

    @property
    def dice(self) -> float:
        total = self.chunks_a + self.chunks_b
        return 2.0 * self.shared_chunks / total if total else 0.0

    @property
    def shared_ratio_small(self) -> float:
        """共享块占较小文件总块数的比例(改动文件对原文件的"保留率")。"""
        small = min(self.chunks_a, self.chunks_b)
        return self.shared_chunks / small if small else 0.0

    @property
    def byte_ratio_small(self) -> float:
        small = min(self.bytes_a, self.bytes_b)
        return self.shared_bytes / small if small else 0.0


@dataclass
class SimilarGroup:
    members: List[int]
    pairs: List[PairSimilarity]

    @property
    def min_dice(self) -> float:
        return min(p.dice for p in self.pairs)

    @property
    def avg_dice(self) -> float:
        return sum(p.dice for p in self.pairs) / len(self.pairs)


class DedupIndex:
    """块倒排索引: chunk_map[digest] = {file_id: 该文件内该块出现次数}。

    索引规模 = O(数据集内不同块总数), 与单个文件大小无关。
    """

    def __init__(self, **chunker_kwargs) -> None:
        self.files: List[FileRecord] = []
        self.chunk_map: Dict[bytes, Dict[int, int]] = {}
        self.chunk_lengths: Dict[bytes, int] = {}
        self.chunker_kwargs = chunker_kwargs

    def add_path(self, path: str) -> FileRecord:
        record = scan_file(path, CDCChunker(**self.chunker_kwargs))
        return self.add_record(record)

    def add_record(self, record: FileRecord) -> FileRecord:
        record.file_id = len(self.files)
        self.files.append(record)
        lengths: Dict[bytes, int] = {}
        for ch in record.chunks:
            lengths.setdefault(ch.digest, ch.length)
        for digest, cnt in record.chunk_counts().items():
            holders = self.chunk_map.get(digest)
            if holders is None:
                self.chunk_map[digest] = {record.file_id: cnt}
                self.chunk_lengths[digest] = lengths[digest]
            else:
                holders[record.file_id] = cnt
        return record

    def num_chunks(self) -> int:
        return sum(len(r.chunks) for r in self.files)

    def num_unique_chunks(self) -> int:
        return len(self.chunk_map)

    # ---- 完全重复组 --------------------------------------------------
    def exact_groups(self) -> List[List[int]]:
        buckets: Dict[str, List[int]] = {}
        for rec in self.files:
            buckets.setdefault(rec.sha256, []).append(rec.file_id)
        return [sorted(ids) for sha, ids in sorted(buckets.items())
                if len(ids) > 1]

    # ---- 两两相似度(只枚举真正共享块的文件对) -------------------------
    def pair_similarities(self) -> Iterator[PairSimilarity]:
        shared: Dict[Tuple[int, int], List[int]] = {}  # (a,b)->[chunks,bytes]
        for digest, holders in self.chunk_map.items():
            if len(holders) < 2:
                continue
            length = self.chunk_lengths[digest]
            items = sorted(holders.items())
            for i in range(len(items)):
                fa, ca = items[i]
                for j in range(i + 1, len(items)):
                    fb, cb = items[j]
                    common = ca if ca < cb else cb
                    key = (fa, fb)
                    slot = shared.get(key)
                    if slot is None:
                        shared[key] = [common, common * length]
                    else:
                        slot[0] += common
                        slot[1] += common * length
        for (fa, fb), (n_shared, b_shared) in shared.items():
            ra, rb = self.files[fa], self.files[fb]
            yield PairSimilarity(
                a=fa, b=fb, shared_chunks=n_shared, shared_bytes=b_shared,
                chunks_a=len(ra.chunks), chunks_b=len(rb.chunks),
                bytes_a=ra.size, bytes_b=rb.size,
            )

    def similar_pairs(self, threshold: float = 0.3) -> List[PairSimilarity]:
        pairs = [p for p in self.pair_similarities() if p.dice >= threshold]
        pairs.sort(key=lambda p: p.dice, reverse=True)
        return pairs

    def similar_groups(self, threshold: float = 0.3) -> List[SimilarGroup]:
        pairs = self.similar_pairs(threshold)
        parent = list(range(len(self.files)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for p in pairs:
            parent[find(p.a)] = find(p.b)

        members_by_root: Dict[int, set] = {}
        for p in pairs:
            root = find(p.a)
            members_by_root.setdefault(root, set()).update((p.a, p.b))
        groups: List[SimilarGroup] = []
        for members_set in members_by_root.values():
            members = sorted(members_set)
            pair_list = [p for p in pairs
                         if p.a in members_set and p.b in members_set]
            groups.append(SimilarGroup(members, pair_list))
        groups.sort(key=lambda g: g.avg_dice, reverse=True)
        return groups

    # ---- 文件内重复块 -------------------------------------------------
    def repeated_chunks(self, file_id: int, min_count: int = 2
                        ) -> List[Tuple[bytes, int, List[int]]]:
        """返回 [(digest, 出现次数, [偏移...])], 按出现次数降序。"""
        rec = self.files[file_id]
        seen: Dict[bytes, List[int]] = {}
        for ch in rec.chunks:
            seen.setdefault(ch.digest, []).append(ch.offset)
        out = [(d, len(offs), offs) for d, offs in seen.items()
               if len(offs) >= min_count]
        out.sort(key=lambda t: -t[1])
        return out


# ----------------------------------------------------------------------
# 目录收集与报告
# ----------------------------------------------------------------------
def collect_files(paths: Iterable[str]) -> List[str]:
    result: List[str] = []
    for p in paths:
        if os.path.isfile(p):
            result.append(p)
        elif os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for name in names:
                    fp = os.path.join(root, name)
                    if os.path.isfile(fp) and not os.path.islink(fp):
                        result.append(fp)
        else:
            raise FileNotFoundError(p)
    return sorted(result)


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PiB"


def scan_paths(paths: Iterable[str], threshold: float = 0.3,
               progress: bool = False, **chunker_kwargs) -> DedupIndex:
    files = collect_files(paths)
    index = DedupIndex(**chunker_kwargs)
    for i, path in enumerate(files, 1):
        index.add_path(path)
        if progress and i % 500 == 0:
            print(f"  ... 已扫描 {i}/{len(files)} 个文件", file=sys.stderr)
    return index


def build_report(index: DedupIndex, threshold: float = 0.3) -> str:
    lines: List[str] = []
    w = lines.append
    w("=" * 74)
    w("文件指纹与分块重复检测报告")
    w("=" * 74)
    total = sum(r.size for r in index.files)
    w(f"文件数: {len(index.files)}    总大小: {_human(total)}")
    w(f"块总数: {index.num_chunks()}    不同块数: {index.num_unique_chunks()}")
    w("")

    exact = index.exact_groups()
    w(f"[完全重复组] 整文件 SHA-256 相同, 共 {len(exact)} 组")
    for gi, members in enumerate(exact, 1):
        size = index.files[members[0]].size
        sha = index.files[members[0]].sha256
        w(f"  组 {gi}: {len(members)} 个文件, 单文件 {_human(size)}, "
          f"可去重节省 {_human(size * (len(members) - 1))}")
        w(f"    sha256={sha[:32]}...")
        for fid in members:
            w(f"    - {index.files[fid].path}")
    w("")

    groups = index.similar_groups(threshold)
    w(f"[相似组] Dice 相似度 >= {threshold}, 共 {len(groups)} 组")
    for gi, group in enumerate(groups, 1):
        w(f"  组 {gi}: {len(group.members)} 个文件, "
          f"组内相似度 {group.min_dice:.3f} ~ {group.avg_dice:.3f}")
        for fid in group.members:
            rec = index.files[fid]
            w(f"    - {rec.path}  ({_human(rec.size)}, {len(rec.chunks)} 块)")
        for p in group.pairs:
            pa, pb = index.files[p.a].path, index.files[p.b].path
            w(f"      相似度(Dice) {p.dice:.3f} | "
              f"共享块 {p.shared_chunks} | "
              f"共享块比例(小文件侧) {p.shared_ratio_small:.3f} | "
              f"共享字节比例(小文件侧) {p.byte_ratio_small:.3f}")
            w(f"        {pa}")
            w(f"        {pb}")
    w("")

    repeated = [(fid, index.repeated_chunks(fid))
                for fid in range(len(index.files))]
    repeated = [(fid, reps) for fid, reps in repeated if reps]
    w(f"[文件内重复块] 共 {len(repeated)} 个文件存在重复块")
    for fid, reps in repeated:
        rec = index.files[fid]
        w(f"  {rec.path}:")
        for digest, count, offsets in reps[:10]:
            length = index.chunk_lengths[digest]
            w(f"    块 {digest.hex()[:16]} 长度 {_human(length)} "
              f"出现 {count} 次, 偏移 {offsets[:8]}"
              f"{' ...' if len(offsets) > 8 else ''}")
        if len(reps) > 10:
            w(f"    ... 另有 {len(reps) - 10} 种重复块")
    return "\n".join(lines)


def report_json(index: DedupIndex, threshold: float = 0.3) -> dict:
    return {
        "num_files": len(index.files),
        "total_bytes": sum(r.size for r in index.files),
        "num_chunks": index.num_chunks(),
        "num_unique_chunks": index.num_unique_chunks(),
        "exact_groups": [
            {"sha256": index.files[m[0]].sha256,
             "size": index.files[m[0]].size,
             "paths": [index.files[fid].path for fid in m]}
            for m in index.exact_groups()
        ],
        "similar_groups": [
            {"members": [index.files[fid].path for fid in g.members],
             "pairs": [
                 {"a": index.files[p.a].path, "b": index.files[p.b].path,
                  "dice": round(p.dice, 4),
                  "shared_chunks": p.shared_chunks,
                  "shared_ratio_small": round(p.shared_ratio_small, 4),
                  "byte_ratio_small": round(p.byte_ratio_small, 4)}
                 for p in g.pairs
             ]}
            for g in index.similar_groups(threshold)
        ],
        "intra_file_duplicates": [
            {"path": index.files[fid].path,
             "chunks": [
                 {"digest": d.hex(), "count": c, "offsets": offs}
                 for d, c, offs in index.repeated_chunks(fid)
             ]}
            for fid in range(len(index.files))
            if index.repeated_chunks(fid)
        ],
    }


# ----------------------------------------------------------------------
# demo: 构造样例数据并输出检测报告
# ----------------------------------------------------------------------
def _make_demo(root: str) -> None:
    rng = random.Random(20260927)
    os.makedirs(root, exist_ok=True)

    def rand(n: int) -> bytes:
        return rng.randbytes(n)

    base = rand(400 * 1024)
    samples = {
        "empty_a.bin": b"",
        "empty_b.bin": b"",
        "tiny.bin": rand(100),
        "report_v1.bin": base,
        "report_v1_copy.bin": base,                                # 完全重复
        "report_v2.bin": rand(4 * 1024) + base,                    # 头部插入
        "report_v3.bin": base[:200 * 1024] + rand(6 * 1024)
                         + base[200 * 1024:],                      # 中间插入
        "random_a.bin": rand(300 * 1024),
        "random_b.bin": rand(300 * 1024),                          # 完全无关
        "self_repeat.bin": rand(50 * 1024) * 8,                    # 文件内重复
    }
    for name, data in samples.items():
        with open(os.path.join(root, name), "wb") as f:
            f.write(data)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chunkdedup",
        description="文件指纹与内容定义分块重复检测")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--min-size", type=int, default=2 * 1024)
        sp.add_argument("--avg-size", type=int, default=8 * 1024)
        sp.add_argument("--max-size", type=int, default=64 * 1024)
        sp.add_argument("--threshold", type=float, default=0.3,
                        help="相似组判定阈值(Dice 相似度)")

    sp_scan = sub.add_parser("scan", help="扫描文件/目录并输出报告")
    sp_scan.add_argument("paths", nargs="+")
    sp_scan.add_argument("--json", dest="json_path", default=None)
    sp_scan.add_argument("--quiet", action="store_true")
    add_common(sp_scan)

    sp_demo = sub.add_parser("demo", help="生成样例数据并输出检测报告")
    sp_demo.add_argument("--dir", default="demo_data")
    add_common(sp_demo)

    args = parser.parse_args(argv)
    chunker_kwargs = {"min_size": args.min_size, "avg_size": args.avg_size,
                      "max_size": args.max_size}

    if args.cmd == "demo":
        _make_demo(args.dir)
        print(f"样例数据已生成于 {args.dir}/", file=sys.stderr)
        index = scan_paths([args.dir], threshold=args.threshold,
                           **chunker_kwargs)
        print(build_report(index, args.threshold))
        return 0

    index = scan_paths(args.paths, threshold=args.threshold,
                       progress=not args.quiet, **chunker_kwargs)
    print(build_report(index, args.threshold))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(report_json(index, args.threshold), f,
                      ensure_ascii=False, indent=2)
        print(f"JSON 报告已写入 {args.json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
