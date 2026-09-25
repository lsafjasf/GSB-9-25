"""面向整数序列的差分编码 + 游程编码库（仅标准库）。

设计要点：
- 所有有符号整数使用「任意精度 zigzag」映射为无符号数，再做 LEB128 变长编码。
  Python int 无位宽限制，zigzag 用算术定义（不用固定位宽移位），
  因此大整数（如 ±2**200）与负数均无损，不存在符号溢出问题。
- 四种模式：RAW（原样）、DELTA（差分）、RLE（游程）、DELTA_RLE（先差分再游程）。
- 自动模式选择：分别试算四种编码的真实字节数，取最小者；
  若所有压缩模式都不小于 RAW，则回退 RAW（防劣化），
  解压端通过头部模式字节自动识别，无需外部约定。

二进制格式：
    MAGIC(4B "RDC1") | mode(1B) | body
body（按模式）：
    RAW:       n | zigzag(v0) zigzag(v1) ...
    DELTA:     n | zigzag(v0) | zigzag(d1) zigzag(d2) ...
    RLE:       n | runs | (zigzag(value), uvarint(count)) * runs
    DELTA_RLE: n | zigzag(v0) | runs | (zigzag(delta), uvarint(count)) * runs
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Sequence, Tuple

MAGIC = b"RDC1"


class Mode(IntEnum):
    RAW = 0
    DELTA = 1
    RLE = 2
    DELTA_RLE = 3


# ---------------------------------------------------------------- zigzag/varint

def zigzag_encode(n: int) -> int:
    """任意精度 zigzag：0,-1,1,-2,2 -> 0,1,2,3,4。纯算术，无位宽假设。"""
    return (n << 1) if n >= 0 else ((-n) << 1) - 1


def zigzag_decode(z: int) -> int:
    return (z >> 1) if (z & 1) == 0 else -((z + 1) >> 1)


def _write_uvarint(buf: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError("uvarint 只接受非负整数")
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def _read_uvarint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("数据被截断：varint 不完整")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


# ---------------------------------------------------------------- 值域变换（列表级）

def delta_encode_values(seq: Sequence[int]) -> List[int]:
    """[v0, v1, ...] -> [v0, v1-v0, v2-v1, ...]"""
    if not seq:
        return []
    out = [seq[0]]
    prev = seq[0]
    for v in seq[1:]:
        out.append(v - prev)
        prev = v
    return out


def delta_decode_values(diff: Sequence[int]) -> List[int]:
    if not diff:
        return []
    out = [diff[0]]
    acc = diff[0]
    for d in diff[1:]:
        acc += d
        out.append(acc)
    return out


def rle_encode_values(seq: Sequence[int]) -> List[Tuple[int, int]]:
    """[a,a,a,b] -> [(a,3),(b,1)]"""
    runs: List[Tuple[int, int]] = []
    for v in seq:
        if runs and runs[-1][0] == v:
            runs[-1] = (v, runs[-1][1] + 1)
        else:
            runs.append((v, 1))
    return runs


def rle_decode_values(runs: Sequence[Tuple[int, int]]) -> List[int]:
    out: List[int] = []
    for v, c in runs:
        if c < 0:
            raise ValueError("游程长度为负")
        out.extend([v] * c)
    return out


def delta_rle_encode_values(seq: Sequence[int]) -> Tuple[List[int], List[Tuple[int, int]]]:
    """返回 (首元素, 差分序列的游程)。"""
    if not seq:
        return [], []
    diff = delta_encode_values(seq)
    return [diff[0]], rle_encode_values(diff[1:])


def delta_rle_decode_values(first: Sequence[int], runs: Sequence[Tuple[int, int]]) -> List[int]:
    if not first:
        return []
    deltas = rle_decode_values(runs)
    return delta_decode_values([first[0]] + deltas)


# ---------------------------------------------------------------- 序列化

def _encode_raw_body(seq: Sequence[int]) -> bytes:
    buf = bytearray()
    _write_uvarint(buf, len(seq))
    for v in seq:
        _write_uvarint(buf, zigzag_encode(v))
    return bytes(buf)


def _encode_delta_body(seq: Sequence[int]) -> bytes:
    buf = bytearray()
    _write_uvarint(buf, len(seq))
    for v in delta_encode_values(seq):
        _write_uvarint(buf, zigzag_encode(v))
    return bytes(buf)


def _encode_rle_body(seq: Sequence[int]) -> bytes:
    runs = rle_encode_values(seq)
    buf = bytearray()
    _write_uvarint(buf, len(seq))
    _write_uvarint(buf, len(runs))
    for v, c in runs:
        _write_uvarint(buf, zigzag_encode(v))
        _write_uvarint(buf, c)
    return bytes(buf)


def _encode_delta_rle_body(seq: Sequence[int]) -> bytes:
    first, runs = delta_rle_encode_values(seq)
    buf = bytearray()
    _write_uvarint(buf, len(seq))
    if first:
        _write_uvarint(buf, zigzag_encode(first[0]))
        _write_uvarint(buf, len(runs))
        for v, c in runs:
            _write_uvarint(buf, zigzag_encode(v))
            _write_uvarint(buf, c)
    return bytes(buf)


_ENCODERS = {
    Mode.RAW: _encode_raw_body,
    Mode.DELTA: _encode_delta_body,
    Mode.RLE: _encode_rle_body,
    Mode.DELTA_RLE: _encode_delta_rle_body,
}


def _decode_raw_body(buf: bytes, pos: int) -> Tuple[List[int], int]:
    n, pos = _read_uvarint(buf, pos)
    out = []
    for _ in range(n):
        z, pos = _read_uvarint(buf, pos)
        out.append(zigzag_decode(z))
    return out, pos


def _decode_delta_body(buf: bytes, pos: int) -> Tuple[List[int], int]:
    n, pos = _read_uvarint(buf, pos)
    diff = []
    for _ in range(n):
        z, pos = _read_uvarint(buf, pos)
        diff.append(zigzag_decode(z))
    return delta_decode_values(diff), pos


def _decode_rle_body(buf: bytes, pos: int) -> Tuple[List[int], int]:
    n, pos = _read_uvarint(buf, pos)
    nruns, pos = _read_uvarint(buf, pos)
    runs = []
    total = 0
    for _ in range(nruns):
        z, pos = _read_uvarint(buf, pos)
        c, pos = _read_uvarint(buf, pos)
        runs.append((zigzag_decode(z), c))
        total += c
    if total != n:
        raise ValueError(f"游程长度之和 {total} 与声明长度 {n} 不一致")
    return rle_decode_values(runs), pos


def _decode_delta_rle_body(buf: bytes, pos: int) -> Tuple[List[int], int]:
    n, pos = _read_uvarint(buf, pos)
    if n == 0:
        return [], pos
    z, pos = _read_uvarint(buf, pos)
    first = zigzag_decode(z)
    nruns, pos = _read_uvarint(buf, pos)
    runs = []
    total = 0
    for _ in range(nruns):
        z, pos = _read_uvarint(buf, pos)
        c, pos = _read_uvarint(buf, pos)
        runs.append((zigzag_decode(z), c))
        total += c
    if total != n - 1:
        raise ValueError(f"差分游程长度之和 {total} 与声明长度 {n - 1} 不一致")
    return delta_rle_decode_values([first], runs), pos


_DECODERS = {
    Mode.RAW: _decode_raw_body,
    Mode.DELTA: _decode_delta_body,
    Mode.RLE: _decode_rle_body,
    Mode.DELTA_RLE: _decode_delta_rle_body,
}


# ---------------------------------------------------------------- 公开 API

@dataclass
class EncodeResult:
    mode: Mode
    data: bytes
    sizes: Dict[str, int] = field(default_factory=dict)
    reason: str = ""

    @property
    def size(self) -> int:
        return len(self.data)


def encode_with_report(seq: Sequence[int], mode="auto") -> EncodeResult:
    """编码并返回选择依据。mode 可为 "auto"/Mode 枚举/模式名小写字符串。"""
    seq = list(seq)
    for v in seq:
        if not isinstance(v, int) or isinstance(v, bool):
            raise TypeError(f"只接受 int 序列，得到 {type(v).__name__}")

    if mode != "auto":
        m = Mode[mode.upper()] if isinstance(mode, str) else Mode(mode)
        data = MAGIC + bytes([m]) + _ENCODERS[m](seq)
        return EncodeResult(m, data, {m.name.lower(): len(data)}, f"手动指定 {m.name}")

    candidates = {m: MAGIC + bytes([m]) + _ENCODERS[m](seq) for m in Mode}
    sizes = {m.name.lower(): len(d) for m, d in candidates.items()}
    best = min(Mode, key=lambda m: len(candidates[m]))
    raw_size = len(candidates[Mode.RAW])
    if best == Mode.RAW:
        reason = f"压缩无收益或劣化，回退原样存储 RAW；各模式字节数 {sizes}"
    else:
        reason = f"{best.name} 体积最小；各模式字节数 {sizes}（RAW 基线 {raw_size}B）"
    return EncodeResult(best, candidates[best], sizes, reason)


def encode(seq: Sequence[int], mode="auto") -> bytes:
    return encode_with_report(seq, mode).data


def decode(data: bytes) -> List[int]:
    """解压；模式由头部字节决定，自动识别 RAW 回退。"""
    if len(data) < len(MAGIC) + 1 or data[: len(MAGIC)] != MAGIC:
        raise ValueError("非法数据：MAGIC 不匹配")
    try:
        mode = Mode(data[len(MAGIC)])
    except ValueError:
        raise ValueError(f"未知模式字节 {data[len(MAGIC)]}") from None
    out, pos = _DECODERS[mode](data, len(MAGIC) + 1)
    if pos != len(data):
        raise ValueError("解码后存在多余字节，数据损坏")
    return out
