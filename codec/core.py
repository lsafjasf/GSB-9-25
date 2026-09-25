"""统一编解码接口。所有模块只允许调用这里。"""
import logging
import struct

from .fields import FIELDS, MAGIC, VERSION, TYPE_NULL

log = logging.getLogger("codec")

_BY_TAG = {f.tag: f for f in FIELDS}

UNKNOWN_KEY = "_unknown"  # 保留未知字段原始 (tag, type_id, payload) 三元组


class Record(dict):
    """解码结果。defaults_applied 记录本次解码按默认值补齐的字段名。"""

    defaults_applied: tuple = ()


def encode(record) -> bytes:
    out = bytearray()
    out += MAGIC
    out.append(VERSION)
    for f in FIELDS:
        if f.name not in record:
            continue
        value = record[f.name]
        out.append(f.tag)
        if value is None:
            out.append(TYPE_NULL)
            out += b"\x00\x00"
        else:
            out.append(f.type_id)
            payload = f.encode_payload(value)
            out += struct.pack(">H", len(payload))
            out += payload
    for tag, type_id, payload in record.get(UNKNOWN_KEY, ()):
        out.append(tag)
        out.append(type_id)
        out += struct.pack(">H", len(payload))
        out += payload
    return bytes(out)


def decode(data: bytes) -> Record:
    if len(data) < 3 or data[:2] != MAGIC:
        raise ValueError("bad magic")
    record = Record()
    unknown = []
    seen = set()
    pos = 3
    while pos + 4 <= len(data):
        tag = data[pos]
        type_id = data[pos + 1]
        (vlen,) = struct.unpack(">H", data[pos + 2:pos + 4])
        payload = data[pos + 4:pos + 4 + vlen]
        if len(payload) != vlen:
            raise ValueError("truncated payload")
        pos += 4 + vlen
        field = _BY_TAG.get(tag)
        if field is not None and type_id == TYPE_NULL:
            record[field.name] = None
            seen.add(field.name)
        elif field is not None and type_id == field.type_id:
            record[field.name] = field.decode_payload(payload)
            seen.add(field.name)
        else:
            # 未知字段或类型不匹配：原样保留，不报错不丢失
            unknown.append((tag, type_id, payload))
    if pos != len(data):
        raise ValueError("trailing garbage")
    defaulted = []
    for f in FIELDS:
        if f.name not in seen:
            record[f.name] = f.default
            defaulted.append(f.name)
    if defaulted:
        record.defaults_applied = tuple(defaulted)
        log.info("missing fields defaulted: %s", ",".join(defaulted))
    if unknown:
        record[UNKNOWN_KEY] = unknown
        log.info("unknown fields preserved: tags=%s", [t for t, _, _ in unknown])
    return record
