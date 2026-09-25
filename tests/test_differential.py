"""字节级差分测试：新实现 vs 两份冻结的历史实现，逐例比较字节。"""
import struct
import unittest

from codec import decode, encode
from codec.core import UNKNOWN_KEY
from legacy import module_a, module_b
from modules import module_a as new_a, module_b as new_b
from tests.gen import corpus, known_fields, strip_unknown


def historical_blobs():
    """手工构造的历史数据：含未知字段、缺失字段、空值。"""
    blobs = []
    # v1 全字段
    blobs.append(
        b"RC\x01"
        + b"\x01\x01" + struct.pack(">H", 4) + struct.pack(">I", 42)
        + b"\x02\x02" + struct.pack(">H", 6) + "张三".encode("utf-8")
        + b"\x03\x03" + struct.pack(">H", 8) + struct.pack(">d", 98.5)
        + b"\x04\x04\x00\x01\x01"
        + b"\x05\x02" + struct.pack(">H", 5) + b"a@b.c"
    )
    # 缺失 email / score，name 为空值
    blobs.append(
        b"RC\x01"
        + b"\x01\x01" + struct.pack(">H", 4) + struct.pack(">I", 7)
        + b"\x02\x05\x00\x00"
        + b"\x04\x04\x00\x01\x00"
    )
    # 带未知字段 tag=200（历史遗留扩展）
    blobs.append(
        b"RC\x01"
        + b"\x01\x01" + struct.pack(">H", 4) + struct.pack(">I", 1)
        + b"\xc8\x02\x00\x04" + b"ext!"
    )
    # 仅头部（全部字段缺失）
    blobs.append(b"RC\x01")
    return blobs


class TestEncodeDifferential(unittest.TestCase):
    def test_encode_bytes_identical_to_legacy(self):
        for i, rec in enumerate(corpus()):
            rec = strip_unknown(rec)  # 历史编码器不认识 _unknown
            expected_a = module_a.encode(rec)
            expected_b = module_b.encode(rec)
            self.assertEqual(expected_a, expected_b, f"legacy A/B 不一致 #{i}")
            with self.subTest(case=i):
                self.assertEqual(encode(rec), expected_a)
                self.assertEqual(new_a.serialize_order(rec), expected_a)
                self.assertEqual(new_b.serialize_report_row(rec), expected_a)

    def test_decode_known_fields_identical_to_legacy(self):
        blobs = historical_blobs()
        rng_corpus = [module_a.encode(strip_unknown(r)) for r in corpus(n=100)]
        for i, blob in enumerate(blobs + rng_corpus):
            with self.subTest(case=i):
                new_rec = decode(blob)
                self.assertEqual(known_fields(new_rec), module_a.decode(blob))
                self.assertEqual(known_fields(new_rec), module_b.decode(blob))
                self.assertEqual(known_fields(new_a.parse_order(blob)), module_a.decode(blob))

    def test_unknown_fields_preserved_not_dropped(self):
        # 历史实现会丢弃未知字段；新实现必须保留原始字节
        blob = historical_blobs()[2]
        rec = decode(blob)
        self.assertEqual(rec[UNKNOWN_KEY], [(200, 2, b"ext!")])
        self.assertTrue(encode(rec).endswith(b"\xc8\x02\x00\x04ext!"))


if __name__ == "__main__":
    unittest.main()
