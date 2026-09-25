"""向前/向后兼容：多余字段保留、缺失字段默认值并记录、空值。"""
import struct
import unittest

from codec import decode, encode
from codec.core import UNKNOWN_KEY


class TestForwardCompat(unittest.TestCase):
    def test_extra_fields_preserved_and_reemitted(self):
        blob = (
            b"RC\x01"
            + b"\x01\x01" + struct.pack(">H", 4) + struct.pack(">I", 9)
            + b"\x02\x02" + struct.pack(">H", 0)
            + b"\x03\x03" + struct.pack(">H", 8) + struct.pack(">d", 1.5)
            + b"\x04\x04" + struct.pack(">H", 1) + b"\x01"
            + b"\x05\x02" + struct.pack(">H", 0)
            + b"\xc8\x02\x00\x03" + b"new"      # 未来版本字段 tag=200
            + b"\xc9\x07\x00\x02" + b"\xde\xad"  # 未知类型也原样保留
        )
        rec = decode(blob)  # 不得报错
        self.assertEqual(rec[UNKNOWN_KEY], [(200, 2, b"new"), (201, 7, b"\xde\xad")])
        self.assertEqual(encode(rec), blob)  # 原样写回，字节不丢

    def test_missing_fields_defaulted_and_logged(self):
        blob = b"RC\x01" + b"\x01\x01" + struct.pack(">H", 4) + struct.pack(">I", 3)
        with self.assertLogs("codec", level="INFO") as cm:
            rec = decode(blob)
        self.assertEqual(rec["user_id"], 3)
        self.assertEqual(rec["name"], "")
        self.assertEqual(rec["score"], 0.0)
        self.assertEqual(rec["active"], False)
        self.assertEqual(rec["email"], "")
        self.assertEqual(
            set(rec.defaults_applied), {"name", "score", "active", "email"}
        )
        self.assertTrue(any("defaulted" in m for m in cm.output))

    def test_null_values_roundtrip(self):
        rec = {"user_id": 1, "name": None, "email": None}
        back = decode(encode(rec))
        self.assertIsNone(back["name"])
        self.assertIsNone(back["email"])
        self.assertEqual(back["user_id"], 1)

    def test_type_mismatch_treated_as_unknown(self):
        # tag=1 本应是 int，却带 string 类型：不得按 int 解析，原样保留
        blob = b"RC\x01" b"\x01\x02\x00\x02hi"
        rec = decode(blob)
        self.assertEqual(rec["user_id"], 0)  # 走默认值
        self.assertEqual(rec[UNKNOWN_KEY], [(1, 2, b"hi")])

    def test_bad_magic_rejected(self):
        with self.assertRaises(ValueError):
            decode(b"XX\x01")


if __name__ == "__main__":
    unittest.main()
