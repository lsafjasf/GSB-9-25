"""dictcodec — 结构化数据字典编码库（仅标准库）。

按列对字符串记录做字典编码：取值 -> 整数编码。编码结果是自包含的
字节串（内嵌字典与存储模式），单独拿到 blob 即可解码。

存储模式（每列独立选择）：
  - dict：字典 + 定宽整数编码，适合取值重复度高的列。
  - raw ：原样存储（varint 长度前缀 + UTF-8 字节），适合取值几乎
          全不相同的列（此时字典反而让体积变大）。

模式选择阈值：对每列精确估算两种模式的序列化代价，取较小者；
相等时选 raw（更简单）。设 n=行数，k=去重后取值数：
  raw_cost  = Σ len(utf8(v))          + Σ varint_len(len(v))   （全部 n 个格子）
  dict_cost = Σ len(utf8(distinct_v)) + Σ varint_len(len(...)) + n * width
  其中 width = 容纳 k 个编码所需的最小定宽字节数（1/2/4/8）。
经验法则：k/n 越小、取值越长，dict 越划算；k 接近 n 时 dict 必亏
（字典里几乎要存全部取值，还要额外付 n*width 的编码费）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

MAGIC = b"DCE1"
FORMAT_VERSION = 1

MODE_RAW = 0
MODE_DICT = 1

MODE_NAMES = {MODE_RAW: "raw", MODE_DICT: "dict"}

__all__ = [
    "encode",
    "decode",
    "encode_with_stats",
    "naive_encode",
    "naive_decode",
    "ColumnStat",
    "EncodeStats",
    "DecodeError",
]


class DecodeError(ValueError):
    """编码数据损坏或格式非法。"""


# ---------------------------------------------------------------------------
# varint（无符号 LEB128）
# ---------------------------------------------------------------------------

def _varint_size(n: int) -> int:
    size = 1
    while n >= 0x80:
        n >>= 7
        size += 1
    return size


def _write_varint(buf: bytearray, n: int) -> None:
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            buf.append(byte | 0x80)
        else:
            buf.append(byte)
            return


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise DecodeError("varint 越界：数据被截断")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise DecodeError("varint 过长")


def _write_bytes(buf: bytearray, payload: bytes) -> None:
    _write_varint(buf, len(payload))
    buf += payload


def _read_bytes(data: bytes, pos: int) -> tuple[bytes, int]:
    length, pos = _read_varint(data, pos)
    end = pos + length
    if end > len(data):
        raise DecodeError("字节段越界：数据被截断")
    return data[pos:end], end


# ---------------------------------------------------------------------------
# 统计信息
# ---------------------------------------------------------------------------

@dataclass
class ColumnStat:
    index: int             # 列下标
    mode: str              # "dict" | "raw"
    rows: int              # 行数
    distinct: int          # 去重后取值数
    dict_bytes: int        # 序列化字典的字节数（raw 模式为 0）
    original_bytes: int    # 原始单元格 UTF-8 字节总数（不含长度前缀）
    raw_cost: int          # 若用 raw 模式的载荷代价（含长度前缀）
    encoded_cost: int      # 实际采用模式的载荷代价（含字典与长度前缀）

    @property
    def saving_ratio(self) -> float:
        """enc/raw，越小越省；1.0 表示与 raw 持平。"""
        if self.raw_cost == 0:
            return 1.0
        return self.encoded_cost / self.raw_cost


@dataclass
class EncodeStats:
    rows: int
    cols: int
    total_bytes: int       # 整个 blob 的字节数
    columns: list[ColumnStat] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [
            f"{'col':>5} {'mode':>5} {'rows':>8} {'distinct':>9} "
            f"{'dict_B':>10} {'orig_B':>12} {'raw_B':>12} {'enc_B':>12} {'enc/raw':>8}"
        ]
        for c in self.columns:
            lines.append(
                f"{c.index:>5} {c.mode:>5} {c.rows:>8} {c.distinct:>9} "
                f"{c.dict_bytes:>10} {c.original_bytes:>12} {c.raw_cost:>12} "
                f"{c.encoded_cost:>12} {c.saving_ratio:>8.3f}"
            )
        lines.append(f"total blob bytes: {self.total_bytes}")
        return lines


# ---------------------------------------------------------------------------
# 列模式代价估算与选择
# ---------------------------------------------------------------------------

def _code_width(k: int) -> int:
    """容纳 k 个编码（0..k-1）所需的定宽字节数（对齐 1/2/4/8）。"""
    if k <= 1:
        return 1
    bits = (k - 1).bit_length()
    width = (bits + 7) // 8
    for w in (1, 2, 4, 8):
        if width <= w:
            return w
    return 8


def _raw_cost(cells: list[bytes]) -> int:
    return sum(len(c) + _varint_size(len(c)) for c in cells)


def _build_dictionary(cells: list[bytes]) -> tuple[list[bytes], list[int]]:
    """按首次出现顺序建字典，返回 (distinct_cells, codes)。"""
    index: dict[bytes, int] = {}
    distinct: list[bytes] = []
    codes: list[int] = []
    for cell in cells:
        code = index.get(cell)
        if code is None:
            code = len(distinct)
            index[cell] = code
            distinct.append(cell)
        codes.append(code)
    return distinct, codes


def _dict_cost(distinct: list[bytes], n_rows: int) -> int:
    width = _code_width(len(distinct))
    entries = sum(len(d) + _varint_size(len(d)) for d in distinct)
    return entries + n_rows * width


def _choose_mode(cells: list[bytes], distinct: list[bytes]) -> tuple[int, int, int]:
    """返回 (mode, raw_cost, dict_cost)。精确估算，取小者，平局选 raw。"""
    raw = _raw_cost(cells)
    dic = _dict_cost(distinct, len(cells))
    if dic < raw:
        return MODE_DICT, raw, dic
    return MODE_RAW, raw, dic


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------

def _validate_rows(rows: Sequence[Sequence[str]]) -> tuple[int, int]:
    n_rows = len(rows)
    if n_rows == 0:
        return 0, 0
    n_cols = len(rows[0])
    for r, row in enumerate(rows):
        if len(row) != n_cols:
            raise ValueError(f"第 {r} 行列数 {len(row)} 与首行 {n_cols} 不一致")
        for c, cell in enumerate(row):
            if not isinstance(cell, str):
                raise TypeError(f"单元格 ({r},{c}) 不是 str：{type(cell).__name__}")
    return n_rows, n_cols


def encode(rows: Sequence[Sequence[str]]) -> bytes:
    """把矩形字符串表编码为自包含字节串。"""
    blob, _ = encode_with_stats(rows)
    return blob


def encode_with_stats(rows: Sequence[Sequence[str]]) -> tuple[bytes, EncodeStats]:
    n_rows, n_cols = _validate_rows(rows)

    buf = bytearray()
    buf += MAGIC
    _write_varint(buf, FORMAT_VERSION)
    _write_varint(buf, n_rows)
    _write_varint(buf, n_cols)

    stats = EncodeStats(rows=n_rows, cols=n_cols, total_bytes=0)

    for col_idx in range(n_cols):
        column = [rows[r][col_idx] for r in range(n_rows)]
        stat = _encode_column(column, buf)
        stat.index = col_idx
        stats.columns.append(stat)

    blob = bytes(buf)
    stats.total_bytes = len(blob)
    return blob, stats


def _encode_column(column: list[str], buf: bytearray) -> ColumnStat:
    cells = [v.encode("utf-8") for v in column]
    distinct, codes = _build_dictionary(cells)
    mode, raw_cost, dic_cost = _choose_mode(cells, distinct)

    original_bytes = sum(len(c) for c in cells)
    dict_bytes = 0

    if mode == MODE_DICT:
        buf.append(MODE_DICT)
        width = _code_width(len(distinct))
        buf.append(width)
        _write_varint(buf, len(distinct))
        dict_start = len(buf)
        for entry in distinct:
            _write_bytes(buf, entry)
        dict_bytes = len(buf) - dict_start
        for code in codes:
            buf += code.to_bytes(width, "big")
        encoded_cost = dic_cost
    else:
        buf.append(MODE_RAW)
        for cell in cells:
            _write_bytes(buf, cell)
        encoded_cost = raw_cost

    return ColumnStat(
        index=-1,  # 由调用方按顺序即列号，这里占位后修正
        mode=MODE_NAMES[mode],
        rows=len(column),
        distinct=len(distinct),
        dict_bytes=dict_bytes,
        original_bytes=original_bytes,
        raw_cost=raw_cost,
        encoded_cost=encoded_cost,
    )


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------

def decode(blob: bytes) -> list[list[str]]:
    """把 encode 产生的字节串解码回矩形字符串表。"""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise TypeError("blob 必须是字节串")
    data = bytes(blob)
    if len(data) < len(MAGIC) or data[: len(MAGIC)] != MAGIC:
        raise DecodeError("magic 不匹配，不是 dictcodec 数据")
    pos = len(MAGIC)
    version, pos = _read_varint(data, pos)
    if version != FORMAT_VERSION:
        raise DecodeError(f"不支持的格式版本：{version}")
    n_rows, pos = _read_varint(data, pos)
    n_cols, pos = _read_varint(data, pos)

    columns: list[list[str]] = []
    for _ in range(n_cols):
        column, pos = _decode_column(data, pos, n_rows)
        columns.append(column)
    if pos != len(data):
        raise DecodeError("解码结束后存在多余字节")

    return [[columns[c][r] for c in range(n_cols)] for r in range(n_rows)]


def _decode_column(data: bytes, pos: int, n_rows: int) -> tuple[list[str], int]:
    if pos >= len(data):
        raise DecodeError("列模式字节缺失")
    mode = data[pos]
    pos += 1

    if mode == MODE_DICT:
        if pos >= len(data):
            raise DecodeError("编码宽度字节缺失")
        width = data[pos]
        pos += 1
        if width not in (1, 2, 4, 8):
            raise DecodeError(f"非法编码宽度：{width}")
        k, pos = _read_varint(data, pos)
        distinct: list[bytes] = []
        for _ in range(k):
            entry, pos = _read_bytes(data, pos)
            distinct.append(entry)
        need = n_rows * width
        if pos + need > len(data):
            raise DecodeError("编码区越界：数据被截断")
        column: list[str] = []
        for i in range(n_rows):
            code = int.from_bytes(data[pos + i * width : pos + (i + 1) * width], "big")
            if code >= k:
                raise DecodeError(f"编码 {code} 超出字典大小 {k}")
            column.append(distinct[code].decode("utf-8"))
        return column, pos + need

    if mode == MODE_RAW:
        column = []
        for _ in range(n_rows):
            cell, pos = _read_bytes(data, pos)
            column.append(cell.decode("utf-8"))
        return column, pos

    raise DecodeError(f"未知列模式：{mode}")


# ---------------------------------------------------------------------------
# 朴素基线（逐格 varint 长度前缀 + UTF-8，行优先），用于对拍与体积对比
# ---------------------------------------------------------------------------

def naive_encode(rows: Sequence[Sequence[str]]) -> bytes:
    n_rows, n_cols = _validate_rows(rows)
    buf = bytearray()
    _write_varint(buf, n_rows)
    _write_varint(buf, n_cols)
    for row in rows:
        for cell in row:
            _write_bytes(buf, cell.encode("utf-8"))
    return bytes(buf)


def naive_decode(blob: bytes) -> list[list[str]]:
    data = bytes(blob)
    n_rows, pos = _read_varint(data, 0)
    n_cols, pos = _read_varint(data, pos)
    rows: list[list[str]] = []
    for _ in range(n_rows):
        row: list[str] = []
        for _ in range(n_cols):
            cell, pos = _read_bytes(data, pos)
            row.append(cell.decode("utf-8"))
        rows.append(row)
    if pos != len(data):
        raise DecodeError("朴素解码结束后存在多余字节")
    return rows
