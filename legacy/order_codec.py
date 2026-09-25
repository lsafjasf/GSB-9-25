"""重构前：订单模块自带的序列化实现（与用户模块重复造轮子）。

注意：本文件是重构前的历史代码，仅作为字节级差分测试的基准保留，
禁止再修改，也禁止新代码引用（测试除外）。
"""
import io
import struct

MAGIC = b"RC"
VERSION = 1

_FIELD_ORDER = ("id", "name", "email", "active", "score", "avatar")


def encode(record):
    out = io.BytesIO()
    out.write(MAGIC)
    out.write(bytes([VERSION]))
    out.write(struct.pack(">H", 6))
    for key in _FIELD_ORDER:
        if key == "id":
            payload = struct.pack(">i", record.get("id", 0))
            tag, type_id = 1, 0x01
        elif key == "name":
            payload = record.get("name", "").encode("utf-8")
            tag, type_id = 2, 0x02
        elif key == "email":
            payload = record.get("email", "").encode("utf-8")
            tag, type_id = 3, 0x02
        elif key == "active":
            payload = b"\x01" if record.get("active", False) else b"\x00"
            tag, type_id = 4, 0x03
        elif key == "score":
            payload = struct.pack(">d", record.get("score", 0.0))
            tag, type_id = 5, 0x04
        else:  # avatar
            payload = record.get("avatar", b"")
            tag, type_id = 6, 0x05
        out.write(bytes([tag, type_id]))
        out.write(struct.pack(">H", len(payload)))
        out.write(payload)
    return out.getvalue()


def decode(data):
    if len(data) < 5 or data[:2] != MAGIC:
        raise ValueError("bad magic")
    stream = io.BytesIO(data)
    stream.read(3)
    (count,) = struct.unpack(">H", stream.read(2))
    rec = {}
    extra = []
    for _ in range(count):
        tag, type_id = stream.read(1)[0], stream.read(1)[0]
        (length,) = struct.unpack(">H", stream.read(2))
        payload = stream.read(length)
        if tag == 1 and type_id == 0x01:
            rec["id"] = struct.unpack(">i", payload)[0]
        elif tag == 2 and type_id == 0x02:
            rec["name"] = payload.decode("utf-8")
        elif tag == 3 and type_id == 0x02:
            rec["email"] = payload.decode("utf-8")
        elif tag == 4 and type_id == 0x03:
            rec["active"] = payload != b"\x00"
        elif tag == 5 and type_id == 0x04:
            rec["score"] = struct.unpack(">d", payload)[0]
        elif tag == 6 and type_id == 0x05:
            rec["avatar"] = payload
        else:
            # 该模块把未知字段塞进 _extra，但编码时不会写回 —— 依然会丢
            extra.append((tag, type_id, payload))
    for key, default in (("id", 0), ("name", ""), ("email", ""),
                         ("active", False), ("score", 0.0), ("avatar", b"")):
        rec.setdefault(key, default)
    if extra:
        rec["_extra"] = extra
    return rec
