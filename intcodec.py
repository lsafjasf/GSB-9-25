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

分块自适应帧（mode = 4, CHUNKED）
---------------------------------
把序列切成若干块，逐块独立选择 RAW/DELTA/RLE/COMBO。
magic | mode=4 | chunk_count(uvarint) | 块表 | 各块 body 顺序拼接
块表每项：mode(1B) + elem_count(uvarint) + body_len(uvarint)
  elem_count = 该块元素个数（>=1），body_len = 该块 body 字节数（>=1）
解码时先校验整块表（含累计元素数与各块偏移），再逐块解码；
块表损坏时报错信息包含块序号与字节偏移，可直接定位。

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

# 分块自适应帧模式（帧级模式，不是块内可选模式）
CHUNKED = 4


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


def _write_body(buf: bytearray, mode: int, seq: Sequence[int]) -> None:
    """写某一元素模式的 body（不含 magic/模式字节），供整帧与分块帧共用。"""
    if mode == RAW:
        _write_raw_body(buf, seq)
    elif mode == DELTA:
        _write_raw_body(buf, delta_encode(seq))
    elif mode == RLE:
        _write_rle_body(buf, rle_encode(seq))
    elif mode == COMBO:
        _write_rle_body(buf, combo_encode(seq))
    else:
        raise CodecError("未知模式: %r" % (mode,))


def _read_body(data: bytes, pos: int, mode: int, end: int = None,
               ctx: str = "") -> Tuple[List[int], int]:
    """从 pos 起解析一个 body，返回 (原序列, 新 pos)。

    end 给定时期望 body 恰好占满 [pos, end)；否则占满到帧尾。
    ctx 为错误定位上下文（如 "块#3"），拼进异常信息。
    """
    if end is None:
        end = len(data)
    if mode in (RAW, DELTA):
        n, pos = read_uvarint(data, pos)
        out = []
        for _ in range(n):
            x, pos = read_svarint(data, pos)
            out.append(x)
        if pos != end:
            raise CodecError("%sbody 未恰好结束（期望结束于偏移 %d，实际 %d）"
                             % (ctx, end, pos))
        return (out if mode == RAW else delta_decode(out)), pos
    if mode in (RLE, COMBO):
        r, pos = read_uvarint(data, pos)
        runs: List[Tuple[int, int]] = []
        prev = None
        for _ in range(r):
            value, pos = read_svarint(data, pos)
            count, pos = read_uvarint(data, pos)
            if count <= 0:
                raise CodecError("%s非法游程计数: %d" % (ctx, count))
            if prev is not None and value == prev:
                raise CodecError("%s相邻游程值相同，帧不合法" % ctx)
            runs.append((value, count))
            prev = value
        if pos != end:
            raise CodecError("%sbody 未恰好结束（期望结束于偏移 %d，实际 %d）"
                             % (ctx, end, pos))
        return (rle_decode(runs) if mode == RLE else combo_decode(runs)), pos
    raise CodecError("%s未知模式: %r" % (ctx, mode))


def serialize(mode: int, seq: Sequence[int]) -> bytes:
    """按指定模式序列化。mode 必须是 RAW/DELTA/RLE/COMBO 之一。"""
    _check_ints(seq)
    if mode not in MODES:
        raise CodecError("未知模式: %r" % (mode,))
    buf = bytearray(MAGIC)
    buf.append(mode)
    _write_body(buf, mode, seq)
    return bytes(buf)


def deserialize(data: bytes) -> Tuple[int, List[int]]:
    """解码帧，返回 (mode, 原序列)。截断 / 脏数据 / 长度不符都会报错。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise CodecError("输入必须是 bytes-like")
    data = bytes(data)
    if len(data) < 5 or data[:4] != MAGIC:
        raise CodecError("magic 不匹配或帧过短")
    mode = data[4]
    if mode == CHUNKED:
        return CHUNKED, _deserialize_chunked(data)
    if mode not in MODES:
        raise CodecError("未知模式字节: %d" % mode)
    seq, pos = _read_body(data, 5, mode)
    if pos != len(data):
        raise CodecError("帧尾部有多余字节")
    return mode, seq


# --------------------------------------------------------------------------
# 分块自适应编码（CHUNKED 帧）
# --------------------------------------------------------------------------

def split_fixed(seq: Sequence[int], chunk_size: int) -> List[Tuple[int, int]]:
    """固定大小切分，返回 [(start, end), ...] 半开区间。空序列返回 []。"""
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) \
            or chunk_size <= 0:
        raise CodecError("chunk_size 必须是正整数: %r" % (chunk_size,))
    n = len(seq)
    return [(i, min(i + chunk_size, n)) for i in range(0, n, chunk_size)]


def split_adaptive(seq: Sequence[int], block_size: int = 1024,
                   max_chunk: int = 1 << 20) -> List[Tuple[int, int]]:
    """自适应切分：先按 block_size 切基础块并逐块选最优模式，
    再把「最优模式相同」的相邻基础块合并为大块（上限 max_chunk 元素）。

    数据分布均匀的段会合并成少数大块（块表开销小），分布突变的
    位置自然成为块边界（每块都能用各自最优模式）。
    """
    n = len(seq)
    if n == 0:
        return []
    blocks = split_fixed(seq, block_size)
    spans: List[Tuple[int, int]] = []
    cur_start, cur_end = blocks[0]
    cur_mode = _best_body_mode(seq[cur_start:cur_end])
    for start, end in blocks[1:]:
        m = _best_body_mode(seq[start:end])
        if m == cur_mode and end - cur_start <= max_chunk:
            cur_end = end
        else:
            spans.append((cur_start, cur_end))
            cur_start, cur_end, cur_mode = start, end, m
    spans.append((cur_start, cur_end))
    return spans


def _best_body_mode(seq: Sequence[int]) -> int:
    """单块最优模式：与 analyze 相同的字节规则（不含帧头），平局 RAW 优先。"""
    sizes = {
        RAW: _raw_body_size(seq),
        DELTA: _raw_body_size(delta_encode(seq)),
        RLE: _rle_body_size(rle_encode(seq)),
        COMBO: _rle_body_size(rle_encode(delta_encode(seq))),
    }
    return min(MODES, key=lambda m: (sizes[m], m))


def serialize_chunked(seq: Sequence[int],
                      spans: Sequence[Tuple[int, int]] = None,
                      chunk_size: int = None,
                      adaptive: bool = False,
                      block_size: int = 1024) -> bytes:
    """分块自适应编码。

    切分方式三选一：
      spans      显式 [(start, end), ...]，必须不重不漏覆盖 [0, len(seq))
      chunk_size 固定块大小
      adaptive=True 自适应切分（block_size 为基础块粒度）
    都不给时按 block_size 固定切分（默认 1024）。
    """
    _check_ints(seq)
    n = len(seq)
    if spans is None:
        if chunk_size is not None:
            spans = split_fixed(seq, chunk_size)
        elif adaptive:
            spans = split_adaptive(seq, block_size)
        else:
            spans = split_fixed(seq, block_size)
    else:
        spans = [(int(s), int(e)) for s, e in spans]
        pos = 0
        for idx, (s, e) in enumerate(spans):
            if s != pos or e <= s or e > n:
                raise CodecError(
                    "块区间非法：块#%d [%d, %d)，期望起点 %d，序列长 %d"
                    % (idx, s, e, pos, n))
            pos = e
        if pos != n:
            raise CodecError("块区间未覆盖全序列：覆盖到 %d，序列长 %d" % (pos, n))

    buf = bytearray(MAGIC)
    buf.append(CHUNKED)
    write_uvarint(buf, len(spans))
    bodies = []
    table = bytearray()
    for s, e in spans:
        chunk = seq[s:e]
        mode = _best_body_mode(chunk)
        body = bytearray()
        _write_body(body, mode, chunk)
        table.append(mode)
        write_uvarint(table, e - s)
        write_uvarint(table, len(body))
        bodies.append(body)
    buf += table
    for body in bodies:
        buf += body
    return bytes(buf)


def parse_chunk_table(data: bytes) -> List[dict]:
    """只解析块表（不解码 body），返回每块的定位信息，供调试/校验。"""
    data = bytes(data)
    if len(data) < 5 or data[:4] != MAGIC:
        raise CodecError("magic 不匹配或帧过短")
    if data[4] != CHUNKED:
        raise CodecError("不是分块帧（模式字节=%d）" % data[4])
    return _read_chunk_table(data)[0]


def _read_chunk_table(data: bytes) -> Tuple[List[dict], int]:
    """解析并校验块表，返回 (表项列表, body 区起始偏移)。

    所有错误信息都带块序号与字节偏移，可直接定位损坏点。
    """
    n = len(data)
    pos = 5
    chunk_count, pos = read_uvarint(data, pos)
    entries: List[dict] = []
    body_off = None
    for i in range(chunk_count):
        entry_pos = pos
        if pos >= n:
            raise CodecError(
                "块表损坏：块#%d 的表项缺失（块表在偏移 %d 处被截断，"
                "帧共 %d 字节，声明 %d 块）" % (i, pos, n, chunk_count))
        mode = data[pos]
        pos += 1
        if mode not in MODES:
            raise CodecError(
                "块表损坏：块#%d 的模式字节非法（值=%d，表项位于偏移 %d）"
                % (i, mode, entry_pos))
        try:
            elem_count, pos = read_uvarint(data, pos)
            body_len, pos = read_uvarint(data, pos)
        except CodecError as exc:
            raise CodecError(
                "块表损坏：块#%d 的表项在偏移 %d 处截断（%s）"
                % (i, entry_pos, exc))
        if elem_count <= 0:
            raise CodecError(
                "块表损坏：块#%d 元素数非法（%d，表项位于偏移 %d）"
                % (i, elem_count, entry_pos))
        if body_len <= 0:
            raise CodecError(
                "块表损坏：块#%d body 长度非法（%d，表项位于偏移 %d）"
                % (i, body_len, entry_pos))
        entries.append({
            "index": i, "mode": mode, "elem_count": elem_count,
            "body_len": body_len, "table_offset": entry_pos,
        })
    body_off = pos
    cursor = body_off
    for entry in entries:
        entry["body_offset"] = cursor
        cursor += entry["body_len"]
        if cursor > n:
            raise CodecError(
                "块表损坏：块#%d 的 body 越界（body 位于偏移 %d..%d，"
                "帧仅 %d 字节）"
                % (entry["index"], entry["body_offset"], cursor, n))
    if cursor != n:
        raise CodecError(
            "块表损坏：块表声明的 body 总长度为 %d 字节，"
            "但帧实际 body 区为 %d 字节（body 区起始偏移 %d）"
            % (cursor - body_off, n - body_off, body_off))
    return entries, body_off


def _deserialize_chunked(data: bytes) -> List[int]:
    entries, _ = _read_chunk_table(data)
    out: List[int] = []
    for entry in entries:
        ctx = "块#%d（body 偏移 %d）：" % (entry["index"], entry["body_offset"])
        try:
            chunk, _ = _read_body(data, entry["body_offset"], entry["mode"],
                                  entry["body_offset"] + entry["body_len"],
                                  ctx)
        except CodecError as exc:
            raise CodecError("块#%d 解码失败：%s" % (entry["index"], exc))
        if len(chunk) != entry["elem_count"]:
            raise CodecError(
                "块#%d 解码出 %d 个元素，与块表声明的 %d 不符（body 偏移 %d）"
                % (entry["index"], len(chunk), entry["elem_count"],
                   entry["body_offset"]))
        out.extend(chunk)
    return out


def encode_chunked(seq: Sequence[int], chunk_size: int = None,
                   adaptive: bool = False,
                   block_size: int = 1024) -> Tuple[bytes, dict]:
    """分块自适应编码，返回 (帧字节, 报告)。

    报告含每块的模式/元素数/字节数与块表开销，便于分析分块数对
    压缩率的影响。
    """
    if chunk_size is not None:
        spans = split_fixed(seq, chunk_size)
    elif adaptive:
        spans = split_adaptive(seq, block_size)
    else:
        spans = split_fixed(seq, block_size)
    blob = serialize_chunked(seq, spans=spans)
    entries = parse_chunk_table(blob)
    # body 区起点即第一块 body_offset；空序列时块表仅 chunk_count 一个字节
    body_start = entries[0]["body_offset"] if entries else 6
    report = {
        "length": len(seq),
        "chunks": len(entries),
        "header_bytes": 5,
        "table_bytes": body_start - 5,
        "body_bytes": len(blob) - body_start,
        "total_bytes": len(blob),
        "chunk_modes": {MODE_NAMES[m]: sum(1 for e in entries if e["mode"] == m)
                        for m in MODES},
        "spans": [(e["index"], spans[e["index"]][0], spans[e["index"]][1])
                  for e in entries],
        "entries": entries,
    }
    return blob, report


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
    if mode not in MODES and mode != CHUNKED:
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
