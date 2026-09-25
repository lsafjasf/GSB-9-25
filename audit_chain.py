"""audit_chain — 审计日志哈希链库（仅 Python 标准库）。

设计要点
--------
* 每条记录 = {seq, prev, data, hash, alg}：
    - seq  : 从 0 开始的连续序号；
    - prev : 前一条记录的摘要（首条为 GENESIS_HASH）；
    - data : 任意可 JSON 序列化的业务内容；
    - hash : sha256(canonical_json({seq, prev, data})) 的十六进制串；
    - alg  : 摘要算法标识，固定为 "sha256"。
* 序列化固定为：json.dumps(..., sort_keys=True, separators=(",", ":"),
  ensure_ascii=False)，UTF-8 编码 —— 字段顺序、分隔符、编码均固定，可重现。
* 追加只允许在末尾进行（append-only 文件，单次 write + fsync），
  历史摘要一经写入绝不覆盖或重写。
* 校验接口 verify() 区分四类问题并报告首处位置：
    - "modified"  内容被修改（存储摘要与内容重算结果不符）
    - "inserted"  条目被插入（含复制记录到别处、拼接外来链）
    - "deleted"   条目被删除（序号出现前向空洞 / 链接跨越）
    - "truncated" 链尾被截断（需要可信锚点 Anchor 才能检出）

威胁模型说明
------------
攻击者不掌握重新计算整条链后“合法化”的能力之外的任何密钥；
若攻击者重算整条链，则只有对比链外可信锚点（Anchor：长度 + 链尾摘要，
应定期签名/外置保存）才能检出。截断攻击本质上只能依靠锚点检出。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, List, Optional

ALGORITHM = "sha256"
GENESIS_HASH = "0" * 64  # 首条记录的 prev

# 校验错误类型
MODIFIED = "modified"
INSERTED = "inserted"
DELETED = "deleted"
TRUNCATED = "truncated"


class CorruptStoreError(Exception):
    """持久化文件损坏（含崩溃残留的撕裂尾行）。"""


def canonical_bytes(seq: int, prev: str, data: Any) -> bytes:
    """固定序列化：键序固定、无空白、UTF-8。"""
    obj = {"seq": seq, "prev": prev, "data": data}
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_hash(seq: int, prev: str, data: Any) -> str:
    return hashlib.sha256(canonical_bytes(seq, prev, data)).hexdigest()


@dataclass(frozen=True)
class Record:
    seq: int
    prev: str
    data: Any
    hash: str
    alg: str = ALGORITHM

    def to_line(self) -> str:
        return json.dumps(
            {"alg": self.alg, "seq": self.seq, "prev": self.prev,
             "data": self.data, "hash": self.hash},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )

    @staticmethod
    def from_line(line: str) -> "Record":
        obj = json.loads(line)
        if obj.get("alg") != ALGORITHM:
            raise CorruptStoreError(f"不支持的摘要算法: {obj.get('alg')!r}")
        return Record(seq=obj["seq"], prev=obj["prev"],
                      data=obj["data"], hash=obj["hash"])


@dataclass(frozen=True)
class Anchor:
    """链外可信锚点：记录数 + 链尾摘要。应定期签名并外置保存。"""
    count: int
    tail_hash: str  # 空链时为 GENESIS_HASH


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    error: Optional[str] = None      # None | modified | inserted | deleted | truncated
    position: Optional[int] = None   # 首处不一致的记录下标（截断时为第一个缺失下标）
    detail: str = ""
    checked: int = 0                 # 本次实际校验的记录数

    def __str__(self) -> str:
        if self.ok:
            return f"OK（校验 {self.checked} 条）"
        return f"{self.error} @ {self.position}: {self.detail}"


class AuditChain:
    """只追加的审计哈希链。可纯内存使用，也可绑定 JSONL 文件持久化。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.records: List[Record] = []
        if path and os.path.exists(path) and os.path.getsize(path) > 0:
            self._load(path)

    def _load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    rec = Record.from_line(line)
                except (ValueError, KeyError, CorruptStoreError) as e:
                    raise CorruptStoreError(
                        f"{path}:{lineno}: 记录损坏（可能是崩溃残留的撕裂尾行）: {e}"
                    ) from None
                self.records.append(rec)

    # ------------------------------------------------------------------ #
    # 追加（仅末尾）
    # ------------------------------------------------------------------ #
    def append(self, data: Any) -> Record:
        """在末尾追加一条记录；追加后立即做 O(1) 增量自检。

        原子性说明：持久化时以 O_APPEND 打开文件，整行一次性 write 后
        flush + fsync。POSIX 下 O_APPEND 的写定位是原子的，不会出现
        与历史数据交错；崩溃最坏只留下一条撕裂的尾行，加载时会被
        CorruptStoreError 检出，可安全截掉该行重试 —— 历史摘要从不重写。
        """
        seq = len(self.records)
        prev = self.records[-1].hash if self.records else GENESIS_HASH
        rec = Record(seq=seq, prev=prev, data=data,
                     hash=compute_hash(seq, prev, data))
        if self.path:
            line = rec.to_line() + "\n"
            with open(self.path, "a", encoding="utf-8") as f:  # O_APPEND
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        # 追加后立即自检：自身摘要 + 与前序的链接
        assert compute_hash(rec.seq, rec.prev, rec.data) == rec.hash
        if self.records:
            assert rec.prev == self.records[-1].hash
            assert rec.seq == self.records[-1].seq + 1
        self.records.append(rec)
        return rec

    # ------------------------------------------------------------------ #
    # 锚点
    # ------------------------------------------------------------------ #
    def anchor(self) -> Anchor:
        tail = self.records[-1].hash if self.records else GENESIS_HASH
        return Anchor(count=len(self.records), tail_hash=tail)

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def verify(self, start: int = 0, stop: Optional[int] = None,
               anchor: Optional[Anchor] = None) -> VerifyResult:
        """校验 [start, stop) 区间；缺省为全链。

        * 完整校验：verify() 或 verify(anchor=trusted)。
        * 增量校验：verify(start, stop) 只校验该区间的内部一致性
          （以 records[start].prev 为区间锚），复杂度 O(stop-start)，
          支持从任意位置开始；区间与全链的信任关系需配合 Anchor 建立。
        """
        recs = self.records
        n = len(recs)
        if stop is None:
            stop = n
        stop = min(stop, n)
        if not (0 <= start <= stop):
            raise ValueError("非法区间")

        # 第一遍：内容完整性 —— 重算每条摘要并与存储值比对
        for i in range(start, stop):
            r = recs[i]
            if r.alg != ALGORITHM or compute_hash(r.seq, r.prev, r.data) != r.hash:
                return VerifyResult(
                    False, MODIFIED, i,
                    f"记录 seq={r.seq} 的存储摘要与内容重算结果不一致（内容被修改）",
                    i - start)

        # 第二遍：序号连续性 + 前向链接
        prev = GENESIS_HASH if start == 0 else recs[start].prev
        expected_seq = 0 if start == 0 else recs[start].seq
        for i in range(start, stop):
            r = recs[i]
            if r.prev != prev:
                # 分类：插入 / 删除
                if i + 1 < stop and recs[i + 1].prev == prev:
                    return VerifyResult(
                        False, INSERTED, i,
                        f"位置 {i} 的记录（seq={r.seq}）为多余条目，"
                        f"其后记录仍链接到位置 {i - 1}（条目被插入）",
                        i - start)
                if r.seq > expected_seq:
                    return VerifyResult(
                        False, DELETED, i,
                        f"序号从 {expected_seq - 1} 跳到 {r.seq}，"
                        f"缺失 seq={expected_seq}..{r.seq - 1}（条目被删除）",
                        i - start)
                if r.seq < expected_seq or (i > start and r.seq == recs[i - 1].seq):
                    return VerifyResult(
                        False, INSERTED, i,
                        f"位置 {i} 出现重复/回退的序号 seq={r.seq}"
                        f"（期望 {expected_seq}），系复制或外来链接入（条目被插入）",
                        i - start)
                if i > start and r.prev == recs[i - 1].prev:
                    return VerifyResult(
                        False, DELETED, i,
                        f"位置 {i} 的 prev 越过了位置 {i - 1}（条目被删除）",
                        i - start)
                # 序号连续但链接断裂：前一条被替换（内容+摘要被重算，链接未同步）
                return VerifyResult(
                    False, MODIFIED, i - 1,
                    f"位置 {i - 1} 的记录被替换（摘要被重算但链接未同步），"
                    f"与位置 {i} 的 prev 断裂（内容被修改）",
                    i - start)
            if r.seq != expected_seq:
                if r.seq > expected_seq:
                    return VerifyResult(
                        False, DELETED, i,
                        f"序号空洞：期望 {expected_seq}，实际 {r.seq}（条目被删除）",
                        i - start)
                return VerifyResult(
                    False, INSERTED, i,
                    f"序号重复/回退：期望 {expected_seq}，实际 {r.seq}（条目被插入）",
                    i - start)
            prev = r.hash
            expected_seq += 1

        # 第三遍：与链外可信锚点比对（仅当校验覆盖到链尾时有意义）
        if anchor is not None and stop == n:
            if n < anchor.count:
                return VerifyResult(
                    False, TRUNCATED, n,
                    f"链长 {n} 小于锚点记录数 {anchor.count}，"
                    f"尾部缺失 {anchor.count - n} 条（链尾被截断）",
                    n - start)
            if n > anchor.count:
                return VerifyResult(
                    False, INSERTED, anchor.count,
                    f"链长 {n} 大于锚点记录数 {anchor.count}，"
                    f"尾部多出 {n - anchor.count} 条（条目被插入）",
                    n - start)
            tail = recs[-1].hash if recs else GENESIS_HASH
            if tail != anchor.tail_hash:
                return VerifyResult(
                    False, MODIFIED, n - 1 if n else 0,
                    "链尾摘要与可信锚点不符（链被整体重算/替换）",
                    n - start)

        return VerifyResult(True, checked=stop - start)
