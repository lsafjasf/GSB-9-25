"""
intcodec.py — 整数序列的差分编码 / 游程编码 / 组合编码库（仅标准库）。

四种模式
--------
RAW   原样存储（ZigZag + LEB128 变长整数）
DELTA 差分序列变长存储
RLE   游程编码（值 + 计数）
COMBO 先差分再游程（对差分序列做 RLE）

帧格式（小端无关、自描述）
--------------------------
magic = b"IC01"（4 字节）
mode  = 1 字节：0=RAW 1=DELTA 2=RLE 3=COMBO
body:
  RAW   : n(uvarint) 后接 n 个 zigzag varint 原值
  DELTA : n + 首值(zigzag) + (n-1) 个 zigzag varint 差分
  RLE   : 游程数 r(uvarint) + r 组 (zigzag 值, uvarint 计数)
  COMBO : 同 RLE，但游程作用在「首值 + 差分」序列上

大整数 / 负数无损：ZigZag 把符号映射到无符号，再用无符号 LEB128；
Python int 为任意精度，解码时显式还原符号，不存在 << 溢出或符号位错误。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

MAGIC = b"IC01"

RAW = 0
DELTA = 1
RLE = 2
COMBO = 3
MODES = (RAW, DELTA, RLE, COMBO)
MODE_NAMES = {RAW: "RAW", DELTA: "DELTA", RLE: "RLE", COMBO: "COMBO"}


class CodecError(ValueError):
    """编码 / 解码错误（含截断帧、非法计数、模式不匹配）。"""


# --------------------------------------------------------------------------
# 底层整数字节编码：ZigZag + 无符号 LEB128
# --------------------------------------------------------------------------

def zigzag_encode(x: int) -> int:
    """0,-1,1,-2,... -> 0,1,2,3,...。

    不依赖任何固定位宽（如 32/64 位掩码），Python 任意精度 int 均无损，
    因此大整数与极端负数不会因位运算出现符号错误。
    """
    if x >= 0:
        return x << 1
    return ((-x) << 1) - 1


# 内部短别名，热点路径少一次函数名查找
_zz = zigzag_encode


def zigzag_decode(u: int) -> int:
    if u < 0:
        raise CodecError("负的无符号整数")
    if u & 1:
        return -((u + 1) >> 1)
    return u >> 1


def uvarint_size(u: int) -> int:
    if u < 0:
        raise CodecError("负的无符号整数")
    if u == 0:
        return 1
    return (u.bit_length() + 6) // 7


def write_uvarint(buf: bytearray, u: int) -> None:
    if u < 0:
        raise CodecError("负的无符号整数")
    while True:
        b = u & 0x7F
        u >>= 7
        if u:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def read_uvarint(data: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    n = len(data)
    while True:
        if pos >= n:
            raise CodecError("uvarint 截断")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        # 上限只用于防御损坏帧的连续 continuation 位；
        # 阈值远大于任何合理整数（约 10KB 一个 varint），不限制合法大整数。
        if shift > 80_000:
            raise CodecError("uvarint 过长")


def write_svarint(buf: bytearray, x: int) -> None:
    write_uvarint(buf, _zz(x))


def read_svarint(data: bytes, pos: int) -> Tuple[int, int]:
    u, pos = read_uvarint(data, pos)
    return zigzag_decode(u), pos


# --------------------------------------------------------------------------
# 元素级变换（编码侧与其逆变换）
# --------------------------------------------------------------------------

def delta_encode(seq: Sequence[int]) -> List[int]:
    """x0, x1-x0, x2-x1, ... （首元素保留原值）。"""
    if not seq:
        return []
    out = [seq[0]]
    prev = seq[0]
    for x in seq[1:]:
        out.append(x - prev)
        prev = x
    return out


def delta_decode(deltas: Sequence[int]) -> List[int]:
    """delta_encode 的逆变换，逐元素累加。"""
    if not deltas:
        return []
    out = [deltas[0]]
    acc = deltas[0]
    for d in deltas[1:]:
        acc += d
        out.append(acc)
    return out


def rle_encode(seq: Sequence[int]) -> List[Tuple[int, int]]:
    """返回 [(值, 计数), ...]，计数均 >=1，且相邻值不同。"""
    runs: List[Tuple[int, int]] = []
    for x in seq:
        if runs and runs[-1][0] == x:
            runs[-1] = (x, runs[-1][1] + 1)
        else:
            runs.append((x, 1))
    return runs


def rle_decode(runs: Sequence[Tuple[int, int]]) -> List[int]:
    """rle_encode 的逆变换；非法计数会抛 CodecError。"""
    out: List[int] = []
    for value, count in runs:
        if not isinstance(count, int) or count <= 0:
            raise CodecError("非法游程计数: %r" % (count,))
        out.extend([value] * count)
    return out


def combo_encode(seq: Sequence[int]) -> List[Tuple[int, int]]:
    """先差分，再对差分序列游程编码。"""
    return rle_encode(delta_encode(seq))


def combo_decode(runs: Sequence[Tuple[int, int]]) -> List[int]:
    """combo_encode 的逆变换：RLE 解码后再做差分逆变换。"""
    return delta_decode(rle_decode(runs))


# --------------------------------------------------------------------------
# 帧序列化
# --------------------------------------------------------------------------

def _check_ints(seq: Sequence[int]) -> None:
    for x in seq:
        if isinstance(x, bool) or not isinstance(x, int):
            raise CodecError("仅支持 int，收到: %r" % (type(x),))


def _write_raw_body(buf: bytearray, seq: Sequence[int]) -> None:
    write_uvarint(buf, len(seq))
    for x in seq:
        write_svarint(buf, x)


def _write_rle_body(buf: bytearray, runs: Sequence[Tuple[int, int]]) -> None:
    write_uvarint(buf, len(runs))
    for value, count in runs:
        write_svarint(buf, value)
        write_uvarint(buf, count)


def serialize(mode: int, seq: Sequence[int]) -> bytes:
    """按指定模式序列化。mode 必须是 RAW/DELTA/RLE/COMBO 之一。"""
    _check_ints(seq)
    if mode not in MODES:
        raise CodecError("未知模式: %r" % (mode,))
    buf = bytearray(MAGIC)
    buf.append(mode)
    if mode == RAW:
        _write_raw_body(buf, seq)
    elif mode == DELTA:
        _write_raw_body(buf, delta_encode(seq))
    elif mode == RLE:
        _write_rle_body(buf, rle_encode(seq))
    else:
        _write_rle_body(buf, combo_encode(seq))
    return bytes(buf)


def deserialize(data: bytes) -> Tuple[int, List[int]]:
    """解码帧，返回 (mode, 原序列)。截断 / 脏数据 / 长度不符都会报错。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise CodecError("输入必须是 bytes-like")
    data = bytes(data)
    if len(data) < 5 or data[:4] != MAGIC:
        raise CodecError("magic 不匹配或帧过短")
    mode = data[4]
    if mode not in MODES:
        raise CodecError("未知模式字节: %d" % mode)
    pos = 5
    if mode in (RAW, DELTA):
        n, pos = read_uvarint(data, pos)
        out = []
        for _ in range(n):
            x, pos = read_svarint(data, pos)
            out.append(x)
        if pos != len(data):
            raise CodecError("帧尾部有多余字节")
        return mode, (out if mode == RAW else delta_decode(out))
    # RLE / COMBO
    r, pos = read_uvarint(data, pos)
    runs: List[Tuple[int, int]] = []
    total = 0
    prev = None
    for _ in range(r):
        value, pos = read_svarint(data, pos)
        count, pos = read_uvarint(data, pos)
        if count <= 0:
            raise CodecError("非法游程计数: %d" % count)
        if prev is not None and value == prev:
            raise CodecError("相邻游程值相同，帧不合法")
        runs.append((value, count))
        total += count
        prev = value
    if pos != len(data):
        raise CodecError("帧尾部有多余字节")
    return mode, (rle_decode(runs) if mode == RLE else combo_decode(runs))


# --------------------------------------------------------------------------
# 体积统计与模式选择（不实际产出整帧，只按同一编码规则计字节）
# --------------------------------------------------------------------------

def _raw_body_size(seq: Sequence[int]) -> int:
    total = uvarint_size(len(seq))
    for x in seq:
        total += uvarint_size(_zz(x))
    return total


def _rle_body_size(runs: Sequence[Tuple[int, int]]) -> int:
    total = uvarint_size(len(runs))
    for value, count in runs:
        total += uvarint_size(_zz(value)) + uvarint_size(count)
    return total


def analyze(seq: Sequence[int]) -> dict:
    """
    分析序列特征并估算四种模式的编码体积（含 5 字节帧头）。

    返回 dict：
      length / runs / longest_run / distinct
      max_abs_delta / small_delta_ratio（|d|<=63 时 varint 仅 1 字节）
      sizes  : 四种模式的实际编码字节数（调用 serialize 真实产出）
      winner : 最优模式名
      mode   : 最优模式常量
      combo_worse_than_raw : 组合模式是否劣化（>= RAW）
      choice : 选择依据（人类可读，中文）
    """
    _check_ints(seq)
    n = len(seq)

    runs = rle_encode(seq)
    deltas = delta_encode(seq)
    delta_runs = rle_encode(deltas)

    longest = max((c for _, c in runs), default=0)
    distinct = len(runs)
    if n >= 2:
        abs_deltas = [abs(d) for d in deltas[1:]]
        max_abs_delta = max(abs_deltas)
        small = sum(1 for d in abs_deltas if d <= 63)
        small_ratio = small / (n - 1)
    else:
        max_abs_delta = 0
        small_ratio = 1.0 if n == 1 else 0.0

    # 与 serialize 完全相同的编码规则，只计字节不产生帧缓冲（大数据上快得多）
    header = len(MAGIC) + 1
    sizes = {
        "RAW": header + _raw_body_size(seq),
        "DELTA": header + _raw_body_size(deltas),
        "RLE": header + _rle_body_size(runs),
        "COMBO": header + _rle_body_size(delta_runs),
    }
    # 平局时按 RAW < DELTA < RLE < COMBO 的优先级（少做事优先）
    mode = min(MODES, key=lambda m: (sizes[MODE_NAMES[m]], m))
    winner = MODE_NAMES[mode]

    combo_bad = sizes["COMBO"] >= sizes["RAW"]
    if combo_bad and winner == "COMBO":  # 理论上不会发生（COMBO>=RAW 时 min 不会选它）
        winner, mode = "RAW", RAW

    reasons = []
    reasons.append(
        "n=%d；游程数=%d（最长游程=%d，不同值=%d）"
        % (n, len(runs), longest, distinct)
    )
    reasons.append(
        "差分：最大|Δ|=%d，单字节差(|Δ|≤63)占比=%.2f" % (max_abs_delta, small_ratio)
    )
    reasons.append(
        "四模式真实体积(含5B帧头) RAW=%d DELTA=%d RLE=%d COMBO=%d"
        % (sizes["RAW"], sizes["DELTA"], sizes["RLE"], sizes["COMBO"])
    )
    if winner == "RAW":
        reasons.append("选 RAW：没有任何变换能小于原样（或并列优先原样）。")
    elif winner == "DELTA":
        reasons.append("选 DELTA：差分集中在小值，varint 变短；且游程不多。")
    elif winner == "RLE":
        reasons.append("选 RLE：长游程多，重复值折叠为 (值,计数) 后最省。")
    else:
        reasons.append("选 COMBO：差分后出现大量相同差分值（平稳/线性段多），RLE 再折叠最省。")
    if combo_bad:
        reasons.append(
            "劣化保护：COMBO=%d ≥ RAW=%d，组合模式在该分布下不划算，"
            "自动模式将回退为 RAW，解压端按帧头模式字节直接识别。"
            % (sizes["COMBO"], sizes["RAW"])
        )

    return {
        "length": n,
        "runs": len(runs),
        "longest_run": longest,
        "distinct": distinct,
        "max_abs_delta": max_abs_delta,
        "small_delta_ratio": round(small_ratio, 4),
        "sizes": sizes,
        "winner": winner,
        "mode": mode,
        "combo_worse_than_raw": combo_bad,
        "choice": " ".join(reasons),
    }


def encode(seq: Sequence[int], mode: int | str = "auto") -> bytes:
    """
    编码。mode 可为 RAW/DELTA/RLE/COMBO 或 "auto"。
    AUTO 选择体积最小者；规则上保证 COMBO 劣化（≥RAW）时绝不选用，
    即对「劣化」情形回退原样存储。
    """
    if isinstance(mode, str):
        if mode != "auto":
            raise CodecError("mode 字符串只支持 'auto'")
        return serialize(analyze(seq)["mode"], seq)
    return serialize(mode, seq)


def encode_auto(seq: Sequence[int]) -> Tuple[bytes, dict]:
    """自动模式编码，同时返回分析/选择依据。"""
    report = analyze(seq)
    return serialize(report["mode"], seq), report


def decode(data: bytes) -> List[int]:
    """根据帧头自描述模式解码。"""
    return deserialize(data)[1]


def decoded_mode(data: bytes) -> int:
    """只读取帧头模式。"""
    data = bytes(data)
    if len(data) < 5 or data[:4] != MAGIC:
        raise CodecError("magic 不匹配或帧过短")
    mode = data[4]
    if mode not in MODES:
        raise CodecError("未知模式字节: %d" % mode)
    return mode


# --------------------------------------------------------------------------
# 极简演示 CLI：python3 intcodec.py "1,2,3,3,3,100,101,102"
# --------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    if len(sys.argv) != 2:
        print('用法: python3 intcodec.py "1,2,3,3,3,100,101,102"')
        sys.exit(2)
    sequence = [int(tok) for tok in sys.argv[1].split(",") if tok.strip() != ""]
    payload, info = encode_auto(sequence)
    print(json.dumps({
        "input": sequence,
        "encoded_hex": payload.hex(),
        "mode_byte": payload[4],
        "decoded": decode(payload),
        "report": info,
    }, ensure_ascii=False, indent=2))
