"""字节级差分测试：重构后的统一编解码 vs 重构前的两份重复实现。

- 编码：同一记录，新接口输出字节必须与 legacy/user_codec、
  legacy/order_codec 逐字节一致；
- 解码：历史 fixtures 与随机字节流，已知字段的解码结果必须与
  legacy 实现完全一致。
"""
import os
import random
import unittest

from legacy import order_codec, user_codec
from src import codec
from src.codec import FIELDS, MISSING_KEY, UNKNOWN_KEY
from tests.gen import random_record

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
CASES = 300


def _known_fields(record):
    return {f.name: record[f.name] for f in FIELDS}


class EncodeDiffTest(unittest.TestCase):
    def test_encode_bytes_identical_to_legacy(self):
        rng = random.Random(20260925)
        for i in range(CASES):
            # 未知字段是重构后新增能力，legacy 不支持，差分只覆盖已知字段
            record = random_record(rng, allow_unknown=False)
            new_bytes = codec.encode_record(record)
            self.assertEqual(new_bytes, user_codec.encode(record), f"case {i} vs user_codec")
            self.assertEqual(new_bytes, order_codec.encode(record), f"case {i} vs order_codec")

    def test_module_wrappers_match_legacy(self):
        from src import order_module, user_module
        rng = random.Random(7)
        for i in range(50):
            record = random_record(rng, allow_unknown=False)
            self.assertEqual(user_module.serialize_user(record), user_codec.encode(record), f"case {i}")
            self.assertEqual(order_module.serialize_order(record), order_codec.encode(record), f"case {i}")


class DecodeDiffTest(unittest.TestCase):
    def test_fixtures_decode_identical_to_legacy(self):
        for name in sorted(os.listdir(FIXTURE_DIR)):
            with open(os.path.join(FIXTURE_DIR, name), "rb") as fh:
                data = fh.read()
            new_rec = codec.decode_record(data)
            self.assertEqual(_known_fields(new_rec), _known_fields(user_codec.decode(data)),
                             f"fixture {name} vs user_codec")
            self.assertEqual(_known_fields(new_rec), _known_fields(order_codec.decode(data)),
                             f"fixture {name} vs order_codec")

    def test_random_streams_decode_identical_to_legacy(self):
        rng = random.Random(99)
        for i in range(CASES):
            record = random_record(rng, allow_unknown=False)
            data = user_codec.encode(record)
            self.assertEqual(_known_fields(codec.decode_record(data)),
                             _known_fields(user_codec.decode(data)), f"case {i}")
            data = order_codec.encode(record)
            self.assertEqual(_known_fields(codec.decode_record(data)),
                             _known_fields(order_codec.decode(data)), f"case {i}")


if __name__ == "__main__":
    unittest.main()
