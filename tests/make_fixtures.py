"""生成历史数据样本 fixtures（用重构前的 legacy 代码产出，模拟线上存量）。

重新生成：python3 -m tests.make_fixtures
"""
import os
import struct

from legacy import order_codec, user_codec

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

FULL_RECORD = {
    "id": 1024,
    "name": "张三/Zhang",
    "email": "zhang@example.com",
    "active": True,
    "score": 98.5,
    "avatar": b"\x89PNG\r\n\x1a\n",
}


def _write(name, data):
    with open(os.path.join(FIXTURE_DIR, name), "wb") as fh:
        fh.write(data)


def main():
    # 1. 当前版本完整记录（用户模块写出）
    _write("user_v1_full.bin", user_codec.encode(FULL_RECORD))

    # 2. 当前版本全默认值记录（订单模块写出）
    _write("order_v1_defaults.bin", order_codec.encode({}))

    # 3. 历史 v0 数据：email 字段（tag 3）尚不存在的年代写出的记录
    v0 = bytearray()
    v0 += b"RC" + b"\x00" + struct.pack(">H", 5)
    v0 += bytes([1, 0x01]) + struct.pack(">H", 4) + struct.pack(">i", 7)
    name = "历史用户".encode("utf-8")
    v0 += bytes([2, 0x02]) + struct.pack(">H", len(name)) + name
    v0 += bytes([4, 0x03]) + struct.pack(">H", 1) + b"\x01"
    v0 += bytes([5, 0x04]) + struct.pack(">H", 8) + struct.pack(">d", 1.25)
    v0 += bytes([6, 0x05]) + struct.pack(">H", 0)
    _write("user_v0_no_email.bin", bytes(v0))

    # 4. “未来”写入方产出的数据：含一个本版本不认识的未知字段 tag=0x63
    data = bytearray(user_codec.encode(FULL_RECORD))
    (count,) = struct.unpack(">H", data[3:5])
    data[3:5] = struct.pack(">H", count + 1)
    payload = b"legacy-extra"
    data += bytes([0x63, 0x05]) + struct.pack(">H", len(payload)) + payload
    _write("user_v1_with_unknown.bin", bytes(data))

    print("fixtures written to", FIXTURE_DIR)


if __name__ == "__main__":
    main()
