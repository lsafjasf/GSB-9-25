"""生成模拟线上存量数据（examples/online_data/），用于演示迁移工具。

    python3 examples/make_online_data.py
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from legacy import user_codec  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(__file__), "online_data")


def _v0_record(record_id, name, active, score):
    """历史 v0 年代写出的记录：还没有 email 字段（tag 3）。"""
    out = bytearray(b"RC\x00" + struct.pack(">H", 5))
    out += bytes([1, 0x01]) + struct.pack(">H", 4) + struct.pack(">i", record_id)
    encoded = name.encode("utf-8")
    out += bytes([2, 0x02]) + struct.pack(">H", len(encoded)) + encoded
    out += bytes([4, 0x03]) + struct.pack(">H", 1) + (b"\x01" if active else b"\x00")
    out += bytes([5, 0x04]) + struct.pack(">H", 8) + struct.pack(">d", score)
    out += bytes([6, 0x05]) + struct.pack(">H", 0)
    return bytes(out)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    report = os.path.join(OUT_DIR, "migrate_report.json")
    if os.path.exists(report):
        os.remove(report)  # 重置为未迁移状态，保证演示可复跑
    records = {
        # 历史 v0 记录（待迁移）
        "user_0001.bin": _v0_record(1, "历史用户甲", True, 61.5),
        "user_0002.bin": _v0_record(2, "历史用户乙", False, 0.0),
        # 当前 v1 记录（应跳过）
        "user_0003.bin": user_codec.encode({"id": 3, "name": "新用户",
                                            "email": "new@example.com"}),
        # 坏记录（应报失败原因）
        "user_0004.bin": b"XX\x01\x00\x00",               # 坏 magic
        "user_0005.bin": _v0_record(5, "截断用户", True, 1.0)[:-4],  # 截断
    }
    for name, data in records.items():
        with open(os.path.join(OUT_DIR, name), "wb") as fh:
            fh.write(data)
    print("sample online data written to", OUT_DIR)


if __name__ == "__main__":
    main()
