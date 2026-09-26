"""audit_chain — 只追加（append-only）的审计日志哈希链库，仅使用 Python 标准库。

设计要点
--------
* 每条记录包含: seq(序号), prev(前一条记录摘要), content_hash(自身内容摘要),
  digest(本条记录摘要), content(业务内容)。
* 摘要算法固定为 SHA-256，带域分离前缀；序列化顺序与字段编码固定：
  - 内容编码: JSON, sort_keys=True, separators=(",", ":"), ensure_ascii=False, UTF-8。
  - 记录摘要: SHA256(DOMAIN | "|R|" | seq(8字节大端) | prev(32字节) | content_hash(32字节))。
  - 落盘格式: 每条记录一行 JSONL，键顺序固定 seq/prev/content_hash/digest/content。
* 追加只允许在末尾进行（O_APPEND 单次 write + fsync），历史摘要永不重写。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

HASH_NAME = "sha256"
GENESIS = b"\x00" * 32
GENESIS_HEX = GENESIS.hex()
_DOMAIN = b"AUDITCHAIN/v1"

# 四类可区分的问题
CONTENT_MODIFIED = "content_modified"  # 内容被修改
ENTRY_INSERTED = "entry_inserted"      # 条目被插入
ENTRY_DELETED = "entry_deleted"        # 条目被删除
TRUNCATED = "truncated"                # 链尾被截断


# ---------------------------------------------------------------- 摘要与编码

def canonical_bytes(content: Any) -> bytes:
    """内容的确定性编码：JSON 排序键、紧凑分隔符、UTF-8。"""
    return json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_digest(content: Any) -> bytes:
    return hashlib.sha256(_DOMAIN + b"|C|" + canonical_bytes(content)).digest()


def record_digest(seq: int, prev: bytes, c_digest: bytes) -> bytes:
    return hashlib.sha256(
        _DOMAIN + b"|R|" + seq.to_bytes(8, "big") + prev + c_digest
    ).digest()


def make_record(seq: int, prev: bytes, content: Any) -> dict:
    c = content_digest(content)
    d = record_digest(seq, prev, c)
    return {
        "seq": seq,
        "prev": prev.hex(),
        "content_hash": c.hex(),
        "digest": d.hex(),
        "content": content,
    }


def serialize_record(rec: dict) -> bytes:
    """固定键序、固定分隔符、UTF-8、LF 结尾 —— 完全可重现。"""
    line = json.dumps(
        {
            "seq": rec["seq"],
            "prev": rec["prev"],
            "content_hash": rec["content_hash"],
            "digest": rec["digest"],
            "content": rec["content"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (line + "\n").encode("utf-8")


# ---------------------------------------------------------------- 校验结果

@dataclass
class VerifyResult:
    ok: bool
    checked: int = 0                    # 本次校验的记录数
    error: Optional[str] = None         # 四类问题之一
    position: Optional[int] = None      # 首处不一致的位置（记录序号/索引）
    detail: str = ""
    head: str = GENESIS_HEX             # 已验证链头（最后一条记录的摘要）

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        if self.ok:
            return f"OK (checked={self.checked}, head={self.head[:16]}…)"
        return (
            f"FAIL error={self.error} position={self.position} "
            f"detail={self.detail}"
        )


def _fail(error: str, position: int, detail: str, checked: int,
          head: str = GENESIS_HEX) -> VerifyResult:
    return VerifyResult(ok=False, checked=checked, error=error,
                        position=position, detail=detail, head=head)


# ---------------------------------------------------------------- 核心校验

def verify_records(
    records: List[dict],
    expected_head: Optional[str] = None,
    start: int = 0,
    known_prev: bytes = GENESIS,
) -> VerifyResult:
    """校验 records[start:]，返回首处不一致的位置与类型。

    - expected_head: 可信的链头摘要（如审计方保存的检查点）。提供时可检出
      “链尾被截断”以及“整链被重算重写”。
    - start/known_prev: 从任意位置增量校验。known_prev 是 records[start-1]
      的可信摘要（start=0 时为 GENESIS）。
    """
    n = len(records)
    digests: List[Optional[bytes]] = [None] * n
    prev = known_prev

    def pred_digest(j: int) -> Optional[bytes]:
        if j == start - 1:
            return known_prev
        if start <= j < n:
            return digests[j]
        return None

    for i in range(start, n):
        r = records[i]
        # 1) 字段完整性
        try:
            seq = int(r["seq"])
            stored_prev = bytes.fromhex(r["prev"])
            stored_ch = r["content_hash"]
            stored_d = r["digest"]
            content = r["content"]
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            return _fail(CONTENT_MODIFIED, i, f"记录字段缺失或畸形: {exc}", i)

        # 2) 重算内容摘要与记录摘要（覆盖 seq/prev/content_hash）
        c = content_digest(content)
        if c.hex() != stored_ch:
            return _fail(CONTENT_MODIFIED, i,
                         "内容摘要不匹配：记录内容被修改", i)
        d = record_digest(seq, stored_prev, c)
        if d.hex() != stored_d:
            return _fail(CONTENT_MODIFIED, i,
                         "记录摘要不匹配：seq/prev/content_hash 被修改", i)
        digests[i] = d

        # 3) 序号连续性（先于链式检查，可直接区分插入/删除）
        if seq != i:
            if seq > i:
                return _fail(
                    ENTRY_DELETED, i,
                    f"序号跳变：期望 {i}，实际 {seq}，缺少 {seq - i} 条", i)
            return _fail(
                ENTRY_INSERTED, i,
                f"序号回退/重复：期望 {i}，实际 {seq}（存在外来或重复条目）", i)

        # 4) 哈希链衔接
        if stored_prev != prev:
            back2 = pred_digest(i - 2)
            if back2 is not None and stored_prev == back2:
                return _fail(ENTRY_DELETED, i - 1,
                             "本条 prev 越过前驱、指向再前一条：前驱被删除", i)
            back1 = pred_digest(i - 1)
            if back1 is not None and i + 1 < n:
                try:
                    nxt_prev = bytes.fromhex(records[i + 1]["prev"])
                except (KeyError, ValueError, TypeError):
                    nxt_prev = None
                if nxt_prev is not None and nxt_prev == back1:
                    return _fail(ENTRY_INSERTED, i,
                                 "后继记录绕过本条直接链接前驱：本条为插入条目", i)
            return _fail(CONTENT_MODIFIED, max(i - 1, 0),
                         f"第 {i - 1} 与 {i} 条之间哈希链断裂"
                         "（被改写、替换或拼接）", i)
        prev = d

    head = prev.hex()
    if expected_head is not None and expected_head != head:
        return _fail(TRUNCATED, n,
                     f"链头 {head[:16]}… 与可信链头 "
                     f"{expected_head[:16]}… 不符：链尾被截断或整链被重算",
                     n, head=head)
    return VerifyResult(ok=True, checked=n - start, head=head)


# ---------------------------------------------------------------- 文件链

class AuditLog:
    """基于 JSONL 文件的只追加审计链。"""

    def __init__(self, path: str):
        self.path = path
        self.records: List[dict] = []
        if os.path.exists(path):
            with open(path, "rb") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.records.append(json.loads(line))

    def __len__(self) -> int:
        return len(self.records)

    @property
    def head(self) -> str:
        return self.records[-1]["digest"] if self.records else GENESIS_HEX

    def checkpoint(self) -> Tuple[int, str]:
        """返回 (长度, 链头摘要)，可作为可信检查点保存。"""
        return len(self.records), self.head

    # -- 追加（原子性说明见 README） --
    def append(self, content: Any) -> dict:
        seq = len(self.records)
        rec = make_record(seq, bytes.fromhex(self.head), content)
        data = serialize_record(rec)
        # O_APPEND：文件偏移的移动与写入是一次不可分操作，并发追加不会交错；
        # 整行通过单次 write(2) 写入，不会写出半行后又被其他写者插入。
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)  # 应答前落盘，保证持久性
        finally:
            os.close(fd)
        # 追加后立即校验（O(1) 增量校验：新记录必须链接到旧链头）
        prev_hex = self.records[-1]["digest"] if self.records else GENESIS_HEX
        assert rec["prev"] == prev_hex, "追加后链式校验失败"
        assert record_digest(rec["seq"], bytes.fromhex(rec["prev"]),
                             bytes.fromhex(rec["content_hash"])).hex() == rec["digest"]
        self.records.append(rec)
        return rec

    # -- 校验 --
    def verify(self, expected_head: Optional[str] = None) -> VerifyResult:
        return verify_records(self.records, expected_head=expected_head)

    def verify_from(self, start: int,
                    known_prev: Optional[bytes] = None) -> VerifyResult:
        """从任意位置增量校验：只需提供 records[start-1] 的可信摘要。"""
        if known_prev is None:
            known_prev = (bytes.fromhex(self.records[start - 1]["digest"])
                          if start > 0 else GENESIS)
        return verify_records(self.records, start=start, known_prev=known_prev)
