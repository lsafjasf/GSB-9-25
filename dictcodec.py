"""dictcodec: 结构化数据逐列字典编码库（仅标准库）。

设计要点
--------
* 每条记录是等长的字符串列表（行），整体是 list[list[str]]（行集）。
* 逐列编码：每列独立选择 "dict"（字典编码）或 "raw"（原样存储）。
* 编码结果自包含：字典、模式标记全部内嵌在字节流里，单独拿到字节即可解码。
* 回退判定：估算 dict 模式体积（字典项 + 定宽编码），不小于 raw 体积时回退 raw。

二进制格式
----------
  magic      : 4 字节  b"DCE1"
  num_rows   : varint
  num_cols   : varint
  随后 num_cols 个列块，每个列块：
    mode     : 1 字节  (0=raw, 1=dict)
    dict 模式:
      dict_size     : varint
      dict 项 * dict_size : varint(字节长) + utf8 字节
      bytes_per_code: 1 字节
      codes         : num_rows 个定宽大端整数
    raw 模式:
      值 * num_rows : varint(字节长) + utf8 字节
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC = b"DCE1"
MODE_RAW = 0
MODE_DICT = 1

# 长度前缀的估算开销（字节）。实际存储用 varint，短值 1 字节，
# 估算时取 4 字节是保守上界，避免对短值列高估字典收益。
LEN_PREFIX_EST = 4


# ---------------------------------------------------------------- varint --

def _write_varint(buf: bytearray, n: int) -> None:
    if n < 0:
        raise ValueError("varint 只支持非负整数")
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def _read_varint(data: bytes | memoryview, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("数据在 varint 中途被截断")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长，数据损坏")


def _write_str(buf: bytearray, s: str) -> None:
    b = s.encode("utf-8")
    _write_varint(buf, len(b))
    buf += b


def _read_str(data: bytes | memoryview, pos: int) -> tuple[str, int]:
    n, pos = _read_varint(data, pos)
    end = pos + n
    if end > len(data):
        raise ValueError("数据在字符串中途被截断")
    return bytes(data[pos:end]).decode("utf-8"), end


# ------------------------------------------------------------ 模式判定 ----

def _bytes_per_code(dict_size: int) -> int:
    """容纳 dict_size 个编码所需的最小整数字节数（至少 1）。"""
    if dict_size <= 1:
        return 1
    return (dict_size - 1).bit_length() // 8 + 1


def estimate_raw_size(values: list[str]) -> int:
    """朴素逐值存储估算：varint 长度前缀 + utf8 字节。"""
    return sum(LEN_PREFIX_EST + len(v.encode("utf-8")) for v in values)


def estimate_dict_size(values: list[str], dict_size: int) -> int:
    """字典编码估算：字典项（长度前缀+字节） + 每行定宽编码。"""
    bpc = _bytes_per_code(dict_size)
    dict_entries = sum(LEN_PREFIX_EST + len(v.encode("utf-8")) for v in set(values))
    return dict_entries + bpc * len(values)


def choose_mode(values: list[str]) -> str:
    """判定阈值与依据：

    直接比较两种模式的估算体积——
      dict 体积 = 去重字典项体积 + 每行 ceil(log256(dict_size)) 字节
      raw  体积 = 每个值 (长度前缀 + utf8 字节)
    当且仅当 dict 估算严格小于 raw 时选 "dict"，否则回退 "raw"。

    依据：字典编码的收益来自重复值被短编码替代；当代价（字典项本身
    要完整存一遍）超过收益时必然劣化。取值几乎全不相同时，
    字典项体积 ≈ raw 体积，还要额外付出每行编码字节，必然回退。
    """
    if not values:
        return MODE_DICT_NAME
    dict_size = len(set(values))
    if estimate_dict_size(values, dict_size) < estimate_raw_size(values):
        return MODE_DICT_NAME
    return MODE_RAW_NAME


MODE_DICT_NAME = "dict"
MODE_RAW_NAME = "raw"


# ---------------------------------------------------------------- 统计 ----

@dataclass
class ColumnStat:
    index: int
    mode: str                 # "dict" | "raw"
    num_rows: int
    dict_size: int            # raw 模式为 0
    raw_bytes: int            # 该列朴素存储体积（估算口径）
    encoded_bytes: int        # 该列在编码结果中的实际字节数

    @property
    def ratio(self) -> float:
        if self.raw_bytes == 0:
            return 0.0
        return self.encoded_bytes / self.raw_bytes


@dataclass
class EncodeStats:
    num_rows: int
    num_cols: int
    columns: list[ColumnStat] = field(default_factory=list)

    @property
    def total_raw(self) -> int:
        return sum(c.raw_bytes for c in self.columns)

    @property
    def total_encoded(self) -> int:
        return sum(c.encoded_bytes for c in self.columns)


# ---------------------------------------------------------------- 编码 ----

def _encode_column(values: list[str]) -> tuple[bytes, ColumnStat, str]:
    """编码一列，返回 (列块字节, 统计, 模式名)。"""
    mode = choose_mode(values)
    raw_est = estimate_raw_size(values)
    buf = bytearray()

    if mode == MODE_DICT_NAME:
        dictionary: dict[str, int] = {}
        codes: list[int] = []
        for v in values:
            idx = dictionary.get(v)
            if idx is None:
                idx = len(dictionary)
                dictionary[v] = idx
            codes.append(idx)
        entries = list(dictionary.keys())
        bpc = _bytes_per_code(len(entries))

        buf.append(MODE_DICT)
        _write_varint(buf, len(entries))
        for e in entries:
            _write_str(buf, e)
        buf.append(bpc)
        for c in codes:
            buf += c.to_bytes(bpc, "big")
        stat_mode = MODE_DICT_NAME
        dict_size = len(entries)
    else:
        buf.append(MODE_RAW)
        for v in values:
            _write_str(buf, v)
        stat_mode = MODE_RAW_NAME
        dict_size = 0

    return bytes(buf), ColumnStat(
        index=-1, mode=stat_mode, num_rows=len(values),
        dict_size=dict_size, raw_bytes=raw_est, encoded_bytes=len(buf),
    ), stat_mode


def encode_with_stats(records: list[list[str]]) -> tuple[bytes, EncodeStats]:
    """编码记录集，返回 (自包含字节流, 统计信息)。"""
    num_rows = len(records)
    num_cols = len(records[0]) if records else 0
    for i, row in enumerate(records):
        if len(row) != num_cols:
            raise ValueError(f"第 {i} 行有 {len(row)} 列，期望 {num_cols} 列")
        for v in row:
            if not isinstance(v, str):
                raise TypeError(f"只支持 str 取值，得到 {type(v).__name__}")

    # 行转列
    columns = [[records[r][c] for r in range(num_rows)] for c in range(num_cols)]

    out = bytearray(MAGIC)
    _write_varint(out, num_rows)
    _write_varint(out, num_cols)

    stats = EncodeStats(num_rows=num_rows, num_cols=num_cols)
    for ci, col in enumerate(columns):
        chunk, stat, _ = _encode_column(col)
        stat.index = ci
        stats.columns.append(stat)
        out += chunk
    return bytes(out), stats


def encode(records: list[list[str]]) -> bytes:
    """编码记录集，返回自包含字节流。"""
    return encode_with_stats(records)[0]


# ---------------------------------------------------------------- 解码 ----

def _decode_column(data: memoryview, pos: int, num_rows: int) -> tuple[list[str], int]:
    if pos >= len(data):
        raise ValueError("数据在列模式标记处被截断")
    mode = data[pos]
    pos += 1

    if mode == MODE_DICT:
        dict_size, pos = _read_varint(data, pos)
        dictionary: list[str] = []
        for _ in range(dict_size):
            s, pos = _read_str(data, pos)
            dictionary.append(s)
        if pos >= len(data):
            raise ValueError("数据在 bytes_per_code 处被截断")
        bpc = data[pos]
        pos += 1
        need = bpc * num_rows
        if pos + need > len(data):
            raise ValueError("数据在编码区中途被截断")
        column = []
        for i in range(num_rows):
            code = int.from_bytes(data[pos + i * bpc: pos + (i + 1) * bpc], "big")
            if code >= dict_size:
                raise ValueError(f"编码 {code} 超出字典大小 {dict_size}")
            column.append(dictionary[code])
        return column, pos + need

    if mode == MODE_RAW:
        column = []
        for _ in range(num_rows):
            s, pos = _read_str(data, pos)
            column.append(s)
        return column, pos

    raise ValueError(f"未知列模式: {mode}")


def decode(data: bytes) -> list[list[str]]:
    """解码（只需编码字节流，字典信息自包含）。"""
    if data[:4] != MAGIC:
        raise ValueError("魔数不匹配，不是 dictcodec 数据")
    view = memoryview(data)
    pos = 4
    num_rows, pos = _read_varint(view, pos)
    num_cols, pos = _read_varint(view, pos)

    columns: list[list[str]] = []
    for _ in range(num_cols):
        col, pos = _decode_column(view, pos, num_rows)
        columns.append(col)
    if pos != len(data):
        raise ValueError("解码结束后有多余字节，数据损坏")

    # 列转行
    return [[columns[c][r] for c in range(num_cols)] for r in range(num_rows)]
