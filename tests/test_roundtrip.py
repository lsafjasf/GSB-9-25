"""往返一致性测试：随机记录 编码 -> 解码，逐字段与期望值相同。

覆盖空值（空串/空 bytes/0/0.0/False）、缺失字段（按默认值补齐）、
未知字段（原始内容保留），并验证二次编码字节稳定。
"""
import random
import unittest

from src import codec
from src.codec import FIELDS, MISSING_KEY, UNKNOWN_KEY
from tests.gen import random_record

CASES = 500


class RoundTripTest(unittest.TestCase):
    def test_roundtrip_field_by_field(self):
        rng = random.Random(123456)
        for i in range(CASES):
            record = random_record(rng)
            decoded = codec.decode_record(codec.encode_record(record))
            for field in FIELDS:
                expected = record.get(field.name, field.default)
                self.assertEqual(decoded[field.name], expected,
                                 f"case {i} field {field.name}")
            # 未知字段原样保留
            self.assertEqual(decoded[UNKNOWN_KEY], record.get(UNKNOWN_KEY, []),
                             f"case {i} unknown fields")
            # 编码时缺失字段已按默认值写出，故解码后 _missing 为空；
            # “缺失字段按默认值处理并记录”针对的是外部历史数据，见 test_compat
            self.assertEqual(decoded[MISSING_KEY], [], f"case {i} missing list")
            # 二次编码字节稳定（含未知字段写回）
            self.assertEqual(codec.encode_record(decoded), codec.encode_record(record),
                             f"case {i} re-encode")

    def test_empty_record_roundtrip(self):
        decoded = codec.decode_record(codec.encode_record({}))
        for field in FIELDS:
            self.assertEqual(decoded[field.name], field.default)
        self.assertEqual(decoded[MISSING_KEY], [])
        self.assertEqual(decoded[UNKNOWN_KEY], [])


if __name__ == "__main__":
    unittest.main()
