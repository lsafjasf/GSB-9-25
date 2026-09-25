"""向前兼容测试：多余字段不报错不丢失、缺失字段按默认值处理并记录。"""
import os
import struct
import unittest

from src import codec
from src.codec import FIELDS, MISSING_KEY, UNKNOWN_KEY

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURE_DIR, name), "rb") as fh:
        return fh.read()


class UnknownFieldTest(unittest.TestCase):
    def test_unknown_field_preserved_and_reencoded(self):
        data = _load("user_v1_with_unknown.bin")
        record = codec.decode_record(data)  # 不得报错
        self.assertEqual(record[UNKNOWN_KEY], [(0x63, 0x05, b"legacy-extra")])
        # 原始内容不丢失：再次编码字节完全一致
        self.assertEqual(codec.encode_record(record), data)

    def test_unknown_field_in_the_middle(self):
        # 未知字段出现在已知字段中间也要保留（写回时统一排在已知字段之后，
        # 内容不丢失，再次解码结果一致）
        tail = codec.encode_record({"id": 1})[5:]
        blob = bytearray(b"RC\x01" + struct.pack(">H", 7))
        blob += bytes([0x63, 0x02]) + struct.pack(">H", 3) + b"abc"
        blob += tail
        record = codec.decode_record(bytes(blob))
        self.assertEqual(record[UNKNOWN_KEY], [(0x63, 0x02, b"abc")])
        self.assertEqual(record["id"], 1)
        reencoded = codec.encode_record(record)
        self.assertIn(b"abc", reencoded)
        again = codec.decode_record(reencoded)
        self.assertEqual(again[UNKNOWN_KEY], [(0x63, 0x02, b"abc")])

    def test_known_tag_with_wrong_type_treated_as_unknown(self):
        # tag 已知但类型不匹配：按未知字段保留，不得按错误类型解码
        blob = bytearray(codec.encode_record({}))
        (count,) = struct.unpack(">H", blob[3:5])
        blob[3:5] = struct.pack(">H", count + 1)
        blob += bytes([1, 0x02]) + struct.pack(">H", 2) + b"hi"  # tag=1 但类型是字符串
        record = codec.decode_record(bytes(blob))
        self.assertEqual(record["id"], 0)  # 走默认值
        self.assertEqual(record[UNKNOWN_KEY], [(1, 0x02, b"hi")])
        self.assertEqual(codec.encode_record(record), bytes(blob))


class MissingFieldTest(unittest.TestCase):
    def test_historical_v0_missing_email(self):
        record = codec.decode_record(_load("user_v0_no_email.bin"))
        self.assertEqual(record["email"], "")          # 缺失字段按默认值
        self.assertEqual(record[MISSING_KEY], ["email"])  # 并记录
        self.assertEqual(record["id"], 7)
        self.assertEqual(record["name"], "历史用户")

    def test_all_fields_missing(self):
        # 字段数为 0 的极端历史数据：全部字段按默认值补齐并记录
        record = codec.decode_record(b"RC\x01\x00\x00")
        self.assertEqual(record[MISSING_KEY], [f.name for f in FIELDS])
        for field in FIELDS:
            self.assertEqual(record[field.name], field.default)

    def test_defaults_fixture_has_no_missing(self):
        # order_v1_defaults.bin 是完整记录（值均为默认值），不算缺失
        record = codec.decode_record(_load("order_v1_defaults.bin"))
        self.assertEqual(record[MISSING_KEY], [])
        for field in FIELDS:
            self.assertEqual(record[field.name], field.default)

    def test_missing_logged(self):
        with self.assertLogs("codec", level="INFO") as cm:
            codec.decode_record(_load("user_v0_no_email.bin"))
        self.assertTrue(any("email" in line for line in cm.output))


class BadDataTest(unittest.TestCase):
    def test_bad_magic_rejected(self):
        with self.assertRaises(ValueError):
            codec.decode_record(b"XX\x01\x00\x00")
        with self.assertRaises(ValueError):
            codec.decode_record(b"")


if __name__ == "__main__":
    unittest.main()
