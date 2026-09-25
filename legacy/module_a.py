"""历史实现 A（冻结，禁止修改）：差分测试的金标准之一。

重构前订单模块内联的序列化代码，逐字段 struct.pack。
"""
import struct

_DEFAULTS = {"user_id": 0, "name": "", "score": 0.0, "active": False, "email": ""}


def encode(rec) -> bytes:
    out = bytearray(b"RC\x01")
    if "user_id" in rec:
        v = rec["user_id"]
        out += b"\x01" + (b"\x05\x00\x00" if v is None else b"\x01" + struct.pack(">H", 4) + struct.pack(">I", v))
    if "name" in rec:
        v = rec["name"]
        out += b"\x02" + (b"\x05\x00\x00" if v is None else b"\x02" + struct.pack(">H", len(v.encode("utf-8"))) + v.encode("utf-8"))
    if "score" in rec:
        v = rec["score"]
        out += b"\x03" + (b"\x05\x00\x00" if v is None else b"\x03" + struct.pack(">H", 8) + struct.pack(">d", v))
    if "active" in rec:
        v = rec["active"]
        out += b"\x04" + (b"\x05\x00\x00" if v is None else b"\x04\x00\x01" + (b"\x01" if v else b"\x00"))
    if "email" in rec:
        v = rec["email"]
        out += b"\x05" + (b"\x05\x00\x00" if v is None else b"\x02" + struct.pack(">H", len(v.encode("utf-8"))) + v.encode("utf-8"))
    return bytes(out)


def decode(data: bytes) -> dict:
    if data[:2] != b"RC":
        raise ValueError("bad magic")
    rec = {}
    pos = 3
    while pos + 4 <= len(data):
        tag, type_id = data[pos], data[pos + 1]
        (vlen,) = struct.unpack(">H", data[pos + 2:pos + 4])
        payload = data[pos + 4:pos + 4 + vlen]
        pos += 4 + vlen
        if type_id == 5:
            if tag == 1: rec["user_id"] = None
            elif tag == 2: rec["name"] = None
            elif tag == 3: rec["score"] = None
            elif tag == 4: rec["active"] = None
            elif tag == 5: rec["email"] = None
            continue
        if tag == 1 and type_id == 1: rec["user_id"] = struct.unpack(">I", payload)[0]
        elif tag == 2 and type_id == 2: rec["name"] = payload.decode("utf-8")
        elif tag == 3 and type_id == 3: rec["score"] = struct.unpack(">d", payload)[0]
        elif tag == 4 and type_id == 4: rec["active"] = payload != b"\x00"
        elif tag == 5 and type_id == 2: rec["email"] = payload.decode("utf-8")
        # 未知字段：历史行为为直接丢弃
    for k, v in _DEFAULTS.items():
        rec.setdefault(k, v)
    return rec
