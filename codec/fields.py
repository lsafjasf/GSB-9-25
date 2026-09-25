"""字段唯一声明处（single source of truth）。

新增 / 删除字段只允许修改本文件。详见 docs/FIELD_CHANGES.md。
"""
from dataclasses import dataclass
from typing import Any, Callable
import struct

MAGIC = b"RC"
VERSION = 1

TYPE_INT = 1    # uint32 大端
TYPE_STR = 2    # utf-8
TYPE_FLOAT = 3  # >d
TYPE_BOOL = 4   # 1 字节
TYPE_NULL = 5   # 空值，无 payload


@dataclass(frozen=True)
class Field:
    tag: int
    name: str
    type_id: int
    default: Any
    encode_payload: Callable[[Any], bytes]
    decode_payload: Callable[[bytes], Any]


def _enc_int(v: int) -> bytes:
    return struct.pack(">I", v)


def _dec_int(b: bytes) -> int:
    return struct.unpack(">I", b)[0]


def _enc_str(v: str) -> bytes:
    return v.encode("utf-8")


def _dec_str(b: bytes) -> str:
    return b.decode("utf-8")


def _enc_float(v: float) -> bytes:
    return struct.pack(">d", v)


def _dec_float(b: bytes) -> float:
    return struct.unpack(">d", b)[0]


def _enc_bool(v: bool) -> bytes:
    return b"\x01" if v else b"\x00"


def _dec_bool(b: bytes) -> bool:
    return b != b"\x00"


# 编码顺序即声明顺序；tag 一旦发布不得复用。
FIELDS = (
    Field(1, "user_id", TYPE_INT, 0, _enc_int, _dec_int),
    Field(2, "name", TYPE_STR, "", _enc_str, _dec_str),
    Field(3, "score", TYPE_FLOAT, 0.0, _enc_float, _dec_float),
    Field(4, "active", TYPE_BOOL, False, _enc_bool, _dec_bool),
    Field(5, "email", TYPE_STR, "", _enc_str, _dec_str),
)
