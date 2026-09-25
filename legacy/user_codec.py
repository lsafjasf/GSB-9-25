"""重构前：用户模块自带的序列化实现。

注意：本文件是重构前的历史代码，仅作为字节级差分测试的基准保留，
禁止再修改，也禁止新代码引用（测试除外）。
"""
import struct

MAGIC = b"RC"
VERSION = 1


def encode(record):
    buf = bytearray()
    buf += MAGIC
    buf += bytes([VERSION])
    buf += struct.pack(">H", 6)

    buf += bytes([1, 0x01]) + struct.pack(">H", 4)
    buf += struct.pack(">i", record.get("id", 0))

    name = record.get("name", "").encode("utf-8")
    buf += bytes([2, 0x02]) + struct.pack(">H", len(name)) + name

    email = record.get("email", "").encode("utf-8")
    buf += bytes([3, 0x02]) + struct.pack(">H", len(email)) + email

    buf += bytes([4, 0x03]) + struct.pack(">H", 1)
    buf += b"\x01" if record.get("active", False) else b"\x00"

    buf += bytes([5, 0x04]) + struct.pack(">H", 8)
    buf += struct.pack(">d", record.get("score", 0.0))

    avatar = record.get("avatar", b"")
    buf += bytes([6, 0x05]) + struct.pack(">H", len(avatar)) + avatar

    return bytes(buf)


def decode(data):
    if len(data) < 5 or data[:2] != MAGIC:
        raise ValueError("bad magic")
    (count,) = struct.unpack(">H", data[3:5])
    pos = 5
    rec = {}
    for _ in range(count):
        tag, type_id = data[pos], data[pos + 1]
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        payload = data[pos + 4:pos + 4 + length]
        pos += 4 + length
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
        # 未知字段：直接丢弃（历史行为，有数据丢失风险）
    rec.setdefault("id", 0)
    rec.setdefault("name", "")
    rec.setdefault("email", "")
    rec.setdefault("active", False)
    rec.setdefault("score", 0.0)
    rec.setdefault("avatar", b"")
    return rec
