"""历史实现 B（冻结，禁止修改）：差分测试的金标准之二。

重构前报表模块内联的序列化代码，bytearray + int.to_bytes 风格，
与 module_a 是同一格式的重复实现。
"""
import struct

_FIELDS = [
    ("user_id", 1, 0),
    ("name", 2, ""),
    ("score", 3, 0.0),
    ("active", 4, False),
    ("email", 5, ""),
]


def _pack(name, value):
    if value is None:
        return 5, b""
    if name in ("name", "email"):
        return 2, value.encode("utf-8")
    if name == "user_id":
        return 1, value.to_bytes(4, "big")
    if name == "score":
        return 3, struct.pack(">d", value)
    return 4, bytes([1 if value else 0])


def encode(rec) -> bytes:
    buf = bytearray(b"RC")
    buf.append(1)
    for name, tag, _default in _FIELDS:
        if name not in rec:
            continue
        type_id, payload = _pack(name, rec[name])
        buf.append(tag)
        buf.append(type_id)
        buf += len(payload).to_bytes(2, "big")
        buf += payload
    return bytes(buf)


def decode(data: bytes) -> dict:
    if data[:2] != b"RC":
        raise ValueError("bad magic")
    rec = {}
    pos = 3
    while pos + 4 <= len(data):
        tag = data[pos]
        type_id = data[pos + 1]
        vlen = int.from_bytes(data[pos + 2:pos + 4], "big")
        payload = data[pos + 4:pos + 4 + vlen]
        pos += 4 + vlen
        for name, ftag, _d in _FIELDS:
            if tag != ftag:
                continue
            if type_id == 5:
                rec[name] = None
            elif name in ("name", "email") and type_id == 2:
                rec[name] = payload.decode("utf-8")
            elif name == "user_id" and type_id == 1:
                rec[name] = int.from_bytes(payload, "big")
            elif name == "score" and type_id == 3:
                rec[name] = struct.unpack(">d", payload)[0]
            elif name == "active" and type_id == 4:
                rec[name] = payload != b"\x00"
    for name, _tag, default in _FIELDS:
        if name not in rec:
            rec[name] = default
    return rec
