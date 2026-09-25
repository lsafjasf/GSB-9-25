"""往返一致性：随机记录 编码->解码 后逐字段相同（含空值/缺失/未知字段）。"""
import unittest

from codec import decode, encode
from codec.core import UNKNOWN_KEY
from codec.fields import FIELDS
from tests.gen import corpus

_BY_NAME = {f.name: f for f in FIELDS}


class TestRoundTrip(unittest.TestCase):
    def test_roundtrip_field_by_field(self):
        for i, rec in enumerate(corpus(n=500)):
            with self.subTest(case=i):
                back = decode(encode(rec))
                for f in FIELDS:
                    if f.name in rec:
                        self.assertEqual(back[f.name], rec[f.name], f.name)
                    else:
                        self.assertEqual(back[f.name], f.default, f"missing {f.name}")
                        self.assertIn(f.name, back.defaults_applied)
                if UNKNOWN_KEY in rec:
                    self.assertEqual(back[UNKNOWN_KEY], rec[UNKNOWN_KEY])
                else:
                    self.assertNotIn(UNKNOWN_KEY, back)

    def test_reencode_is_byte_stable(self):
        # 缺失字段经一次解码会被默认值补齐；补齐后的结果必须再编解码字节稳定
        for i, rec in enumerate(corpus(n=200)):
            with self.subTest(case=i):
                normalized = encode(decode(encode(rec)))
                self.assertEqual(encode(decode(normalized)), normalized)

    def test_reencode_identical_when_no_field_missing(self):
        for i, rec in enumerate(corpus(n=200)):
            if len(rec.get("_unknown", ())) == 0 and all(f.name in rec for f in FIELDS):
                with self.subTest(case=i):
                    self.assertEqual(encode(decode(encode(rec))), encode(rec))


if __name__ == "__main__":
    unittest.main()
