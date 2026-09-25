"""统一记录编解码接口（重构后全系统唯一实现）。

线上字节格式（重构前后完全一致，禁止变更）::

    偏移  内容
    0     magic: 2 字节 b"RC"
    2     version: 1 字节，当前为 1
    3     field_count: 2 字节大端无符号整数
    5     重复 field_count 次的字段项：
            tag:     1 字节字段编号
            type:    1 字节类型编号
            length:  2 字节大端 payload 长度
            payload: length 字节

类型编号：0x01=int32  0x02=utf-8 字符串  0x03=bool  0x04=float64  0x05=bytes

★ 字段增删的唯一改动点：下方 FIELDS 元组。新增字段只需在其中追加一行
  Field(...)，tag 取未用过的新编号；编码顺序、默认值、解码、测试全部
  自动生效，业务模块与测试代码无需任何改动。

解码约定（向前兼容）：
- 未知 tag 或类型不匹配的字段不报错、不丢弃，原始 (tag, type, payload)
  原样保存在返回 dict 的 "_unknown" 列表中，再次编码时原样写回；
- 缺失字段按 FIELDS 中声明的 default 补齐，字段名记录在 "_missing" 中
  并输出日志。
"""
import logging
import struct
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger("codec")

MAGIC = b"RC"
VERSION = 1

T_INT32 = 0x01
T_STR = 0x02
T_BOOL = 0x03
T_FLOAT64 = 0x04
T_BYTES = 0x05

#: decode 返回 dict 中存放未知字段原始内容的键：[(tag, type_id, payload), ...]
UNKNOWN_KEY = "_unknown"
#: decode 返回 dict 中存放缺失（已按默认值补齐）字段名的键：[name, ...]
MISSING_KEY = "_missing"


def _enc_int32(value: int) -> bytes:
    return struct.pack(">i", value)


def _dec_int32(payload: bytes) -> int:
    return struct.unpack(">i", payload)[0]


def _enc_str(value: str) -> bytes:
    return value.encode("utf-8")


def _dec_str(payload: bytes) -> str:
    return payload.decode("utf-8")


def _enc_bool(value: bool) -> bytes:
    return b"\x01" if value else b"\x00"


def _dec_bool(payload: bytes) -> bool:
    return payload != b"\x00"


def _enc_float64(value: float) -> bytes:
    return struct.pack(">d", value)


def _dec_float64(payload: bytes) -> float:
    return struct.unpack(">d", payload)[0]


def _enc_bytes(value: bytes) -> bytes:
    return value


def _dec_bytes(payload: bytes) -> bytes:
    return payload


@dataclass(frozen=True)
class Field:
    """单个字段的全部声明：名字、线上 tag、类型、默认值、编解码函数。"""

    name: str
    tag: int
    type_id: int
    default: Any
    encode: Callable[[Any], bytes]
    decode: Callable[[bytes], Any]


# ---------------------------------------------------------------------------
# ★★★ 字段映射的唯一声明点：增删字段只允许修改这里 ★★★
# 顺序即线上字节流中的字段顺序，禁止重排已有条目；新增字段只能追加。
# ---------------------------------------------------------------------------
FIELDS: Tuple[Field, ...] = (
    Field("id",     1, T_INT32,   0,     _enc_int32,   _dec_int32),
    Field("name",   2, T_STR,     "",    _enc_str,     _dec_str),
    Field("email",  3, T_STR,     "",    _enc_str,     _dec_str),
    Field("active", 4, T_BOOL,    False, _enc_bool,    _dec_bool),
    Field("score",  5, T_FLOAT64, 0.0,   _enc_float64, _dec_float64),
    Field("avatar", 6, T_BYTES,   b"",   _enc_bytes,   _dec_bytes),
)

_FIELDS_BY_TAG: Dict[int, Field] = {f.tag: f for f in FIELDS}


def _write_field(out: bytearray, tag: int, type_id: int, payload: bytes) -> None:
    out.append(tag)
    out.append(type_id)
    out += struct.pack(">H", len(payload))
    out += payload


def encode_record(record: Dict[str, Any]) -> bytes:
    """把记录 dict 编码为线上字节格式。

    已知字段按 FIELDS 声明顺序写出，缺失的取默认值；record["_unknown"]
    中保留的未知字段原样追加在末尾。
    """
    unknown: List[Tuple[int, int, bytes]] = list(record.get(UNKNOWN_KEY, ()))
    out = bytearray()
    out += MAGIC
    out.append(VERSION)
    out += struct.pack(">H", len(FIELDS) + len(unknown))
    for field in FIELDS:
        value = record.get(field.name, field.default)
        _write_field(out, field.tag, field.type_id, field.encode(value))
    for tag, type_id, payload in unknown:
        _write_field(out, tag, type_id, payload)
    return bytes(out)


def decode_record(data: bytes) -> Dict[str, Any]:
    """把线上字节解码为记录 dict。

    已知字段解码为对应类型的值；未知字段原始内容保留在 "_unknown"；
    缺失字段按默认值补齐，字段名记录在 "_missing" 并写日志。
    """
    if len(data) < 5 or data[:2] != MAGIC:
        raise ValueError("bad magic")
    (count,) = struct.unpack(">H", data[3:5])
    pos = 5
    record: Dict[str, Any] = {}
    unknown: List[Tuple[int, int, bytes]] = []
    seen = set()
    for _ in range(count):
        tag, type_id = data[pos], data[pos + 1]
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        payload = bytes(data[pos + 4:pos + 4 + length])
        pos += 4 + length
        field = _FIELDS_BY_TAG.get(tag)
        if field is None or field.type_id != type_id:
            unknown.append((tag, type_id, payload))
            continue
        record[field.name] = field.decode(payload)
        seen.add(field.name)
    missing = [f.name for f in FIELDS if f.name not in seen]
    for field in FIELDS:
        if field.name not in seen:
            record[field.name] = field.default
    if missing:
        logger.info("missing fields filled with defaults: %s", missing)
    record[MISSING_KEY] = missing
    record[UNKNOWN_KEY] = unknown
    return record
