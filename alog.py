"""alog - 追加式日志格式的读写库（仅标准库）。

记录格式（小端）：
    +--------+----------+-------+==========+
    | magic4 | len u32  |crc u32| payload  |
    +--------+----------+-------+==========+
    magic  = b'\\x89ALG'，用于重同步扫描
    len    = payload 字节数，上限 MAX_RECORD_SIZE
    crc    = zlib.crc32(payload)

损坏分类（Corruption.kind）：
    truncated_tail     文件末尾的半截记录（写进程被强杀的典型现场）
    invalid_length     长度字段非法（超过上限）
    checksum_mismatch  长度合法但 CRC 校验失败（内容被翻转 / 长度与实际不符）
    bad_magic          魔数不匹配（头部本身被改写）
"""

from __future__ import annotations

import mmap
import os
import struct
import zlib
from dataclasses import dataclass, field

MAGIC = b"\x89ALG"
HEADER = struct.Struct("<4sII")          # magic, length, crc32
HEADER_SIZE = HEADER.size                # 12
MAX_RECORD_SIZE = 64 * 1024 * 1024       # 单条记录 payload 上限 64 MiB

KIND_TRUNCATED_TAIL = "truncated_tail"
KIND_INVALID_LENGTH = "invalid_length"
KIND_CHECKSUM_MISMATCH = "checksum_mismatch"
KIND_BAD_MAGIC = "bad_magic"

MODE_STRICT = "strict"   # 遇到损坏即停止并报告
MODE_SKIP = "skip"       # 跳过损坏区域继续读到文件末尾

_WRITE_BUFFER_SIZE = 4 * 1024 * 1024


# ---------------------------------------------------------------- 数据结构

@dataclass
class Corruption:
    """一处被检出的损坏。"""
    kind: str          # 上述 KIND_* 之一
    offset: int        # 损坏记录（或损坏区域）的起始偏移
    reason: str        # 人类可读的原因说明

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"[{self.kind}] @offset={self.offset}: {self.reason}"


@dataclass
class WriteAck:
    """一次批量追加的确认。

    buffered_end   本次写入后「写入缓冲区」的末尾偏移（未必落盘）
    durable_offset 截至当前「已强制落盘」的位置（<= buffered_end）
    """
    buffered_end: int
    durable_offset: int

    @property
    def durable(self) -> bool:
        """本次写入是否已全部落盘。"""
        return self.buffered_end <= self.durable_offset


class CorruptionError(Exception):
    """strict 模式下遇到损坏时抛出。"""

    def __init__(self, corruption: Corruption):
        self.corruption = corruption
        super().__init__(str(corruption))


@dataclass
class ReadReport:
    """一次完整读取的汇总。"""
    records: int = 0
    payload_bytes: int = 0
    corruptions: list = field(default_factory=list)   # list[Corruption]
    skipped: list = field(default_factory=list)       # list[(start, end)] 跳过的字节区间
    clean: bool = True                                 # 是否全程无损坏


# ---------------------------------------------------------------- 写入方

class LogWriter:
    """追加式写入。区分两级确认：

    - append_batch() 返回的 WriteAck 只保证进入「写入缓冲区」；
    - 调用 fsync() 后才推进「已强制落盘」位置，可通过
      ack.durable_offset / writer.durable_offset 拿到最后落盘位置。
    """

    def __init__(self, path: str):
        self._path = path
        self._f = open(path, "ab", buffering=_WRITE_BUFFER_SIZE)
        self._offset = os.path.getsize(path)
        # 已存在于文件中的字节视为落盘（由上一个写入者负责 fsync）
        self._durable = self._offset
        self._closed = False

    # -- 写入 --
    def append(self, record: bytes) -> WriteAck:
        return self.append_batch([record])

    def append_batch(self, records) -> WriteAck:
        """批量追加。返回的 ack 仅表示进入写缓冲区。"""
        if self._closed:
            raise ValueError("writer already closed")
        buf = bytearray()
        for rec in records:
            rec = bytes(rec)
            if len(rec) > MAX_RECORD_SIZE:
                raise ValueError(f"record too large: {len(rec)} > {MAX_RECORD_SIZE}")
            buf += HEADER.pack(MAGIC, len(rec), zlib.crc32(rec))
            buf += rec
        self._f.write(buf)
        self._offset += len(buf)
        return WriteAck(buffered_end=self._offset, durable_offset=self._durable)

    # -- 落盘 --
    def fsync(self) -> int:
        """冲刷缓冲区并 fsync，返回最后落盘位置（字节偏移）。"""
        if self._closed:
            raise ValueError("writer already closed")
        self._f.flush()
        os.fsync(self._f.fileno())
        self._durable = self._offset
        return self._durable

    @property
    def durable_offset(self) -> int:
        """最后落盘位置。"""
        return self._durable

    @property
    def buffered_offset(self) -> int:
        """写缓冲区末尾位置。"""
        return self._offset

    def close(self, fsync: bool = True):
        if not self._closed:
            if fsync:
                self.fsync()
            self._f.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------- 读取方

class LogReader:
    """逐条校验的读取器。

    mode = MODE_STRICT: 遇到损坏记录进 corruptions 后抛 CorruptionError。
    mode = MODE_SKIP  : 跳过损坏区域继续读到文件末尾，
                        跳过的内容与范围记录在 corruptions / skipped 中。
    """

    def __init__(self, path: str, mode: str = MODE_STRICT,
                 max_record_size: int = MAX_RECORD_SIZE):
        if mode not in (MODE_STRICT, MODE_SKIP):
            raise ValueError(f"unknown mode: {mode!r}")
        self.path = path
        self.mode = mode
        self.max_record_size = max_record_size
        self.corruptions: list = []   # list[Corruption]
        self.skipped: list = []       # list[(start, end)]

    # -- 内部 --
    def _report(self, corruption: Corruption):
        self.corruptions.append(corruption)
        if self.mode == MODE_STRICT:
            raise CorruptionError(corruption)

    def _note_skipped(self, start: int, end: int):
        if end <= start:
            return
        if self.skipped and self.skipped[-1][1] == start:
            self.skipped[-1] = (self.skipped[-1][0], end)  # 合并相邻区间
        else:
            self.skipped.append((start, end))

    def _classify(self, mm: mmap.mmap, size: int, pos: int):
        """检查 pos 处是否是一条完整且校验通过的记录。

        返回 None 表示合法；否则返回 Corruption（不抛出、不记录）。
        """
        if size - pos < HEADER_SIZE:
            return Corruption(
                KIND_TRUNCATED_TAIL, pos,
                f"文件末尾仅剩 {size - pos} 字节，"
                f"不足一个记录头（{HEADER_SIZE} 字节）")
        magic, length, crc = HEADER.unpack_from(mm, pos)
        if magic != MAGIC:
            return Corruption(KIND_BAD_MAGIC, pos,
                              f"魔数不匹配：{bytes(magic)!r}")
        if length > self.max_record_size:
            return Corruption(
                KIND_INVALID_LENGTH, pos,
                f"长度字段 {length} 超过上限 {self.max_record_size}")
        end = pos + HEADER_SIZE + length
        if end > size:
            return Corruption(
                KIND_TRUNCATED_TAIL, pos,
                f"记录声明长度 {length}，但文件在 "
                f"{size - pos - HEADER_SIZE} 字节处结束")
        view = memoryview(mm)[pos + HEADER_SIZE:end]
        ok = zlib.crc32(view) == crc
        view.release()
        if not ok:
            return Corruption(KIND_CHECKSUM_MISMATCH, pos,
                              f"CRC 校验失败（记录长度 {length}）")
        return None

    def _resync(self, mm: mmap.mmap, size: int, start: int) -> int:
        """skip 模式：从 start 起扫描下一条合法记录的位置。

        扫描途中遇到的每个候选损坏都会逐一分类记录（连续多处损坏
        不会被静默合并）；找不到合法记录则返回 size。
        """
        idx = mm.find(MAGIC, start)
        while idx != -1:
            c = self._classify(mm, size, idx)
            if c is None:
                return idx
            self._report(c)
            if c.kind == KIND_TRUNCATED_TAIL:
                return size
            idx = mm.find(MAGIC, idx + 1)
        return size

    # -- 迭代 --
    def __iter__(self):
        """产出 (offset, payload_bytes)。迭代结束后读取 corruptions/skipped。"""
        size = os.path.getsize(self.path)
        if size == 0:
            return  # 空文件：干净结束
        with open(self.path, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                pos = 0
                while pos < size:
                    c = self._classify(mm, size, pos)
                    if c is None:
                        length = HEADER.unpack_from(mm, pos)[1]
                        payload = bytes(mm[pos + HEADER_SIZE:
                                           pos + HEADER_SIZE + length])
                        yield pos, payload
                        pos += HEADER_SIZE + length
                        continue
                    self._report(c)
                    if c.kind == KIND_TRUNCATED_TAIL:
                        # 末尾半截：后面不可能再有数据，直接结束
                        self._note_skipped(pos, size)
                        break
                    # skip 模式：重同步到下一个合法记录
                    nxt = self._resync(mm, size, pos + 1)
                    self._note_skipped(pos, nxt)
                    pos = nxt


def read_all(path: str, mode: str = MODE_STRICT) -> tuple:
    """便捷接口：读完全部记录，返回 ([(offset, payload)], ReadReport)。"""
    reader = LogReader(path, mode=mode)
    records = []
    report = ReadReport()
    try:
        for off, payload in reader:
            records.append((off, payload))
            report.records += 1
            report.payload_bytes += len(payload)
    except CorruptionError:
        pass
    report.corruptions = reader.corruptions
    report.skipped = reader.skipped
    report.clean = not reader.corruptions
    return records, report
