"""test_dictcodec.py: 对拍测试 —— 解码结果必须与原始记录逐格一致。

运行: python3 -m unittest test_dictcodec -v
"""

import random
import string
import unittest

from dictcodec import (
    MODE_DICT_NAME,
    MODE_RAW_NAME,
    choose_mode,
    decode,
    encode,
    encode_with_stats,
)


def naive_store(records: list[list[str]]) -> list[list[str]]:
    """逐列朴素存储的参照实现：逐格 utf-8 落盘再读回。"""
    if not records:
        return []
    num_cols = len(records[0])
    columns = [[row[c] for row in records] for c in range(num_cols)]
    restored = []
    for col in columns:
        blob = b"".join(
            len(v.encode("utf-8")).to_bytes(4, "big") + v.encode("utf-8")
            for v in col
        )
        out, pos = [], 0
        while pos < len(blob):
            n = int.from_bytes(blob[pos:pos + 4], "big")
            pos += 4
            out.append(blob[pos:pos + n].decode("utf-8"))
            pos += n
        restored.append(out)
    return [[restored[c][r] for c in range(num_cols)]
            for r in range(len(records))]


def assert_roundtrip(testcase, records):
    blob = encode(records)
    testcase.assertEqual(decode(blob), records, "解码结果与原始记录不一致")
    testcase.assertEqual(decode(blob), naive_store(records),
                         "与朴素存储参照结果不一致")
    return blob


class RoundTripTest(unittest.TestCase):
    def test_empty_dataset_zero_rows(self):
        assert_roundtrip(self, [])

    def test_empty_column_all_empty_strings(self):
        records = [["", ""] for _ in range(100)]
        assert_roundtrip(self, records)

    def test_single_row(self):
        assert_roundtrip(self, [["only"]])

    def test_single_value_column(self):
        records = [["CONST", str(i)] for i in range(1000)]
        blob, stats = encode_with_stats(records)
        self.assertEqual(stats.columns[0].mode, MODE_DICT_NAME)
        self.assertEqual(stats.columns[0].dict_size, 1)
        self.assertEqual(decode(blob), records)

    def test_highly_repetitive(self):
        rng = random.Random(1)
        values = ["ACTIVE", "PENDING", "CLOSED"]
        records = [[rng.choice(values), rng.choice("AB")] for _ in range(20000)]
        blob, stats = encode_with_stats(records)
        self.assertTrue(all(c.mode == MODE_DICT_NAME for c in stats.columns))
        # 2 列 x 20000 行，1 字节编码 -> 约 40000 字节；
        # 朴素存储约 20000*(7+1+1+4)*2 远超 10 万
        self.assertLess(len(blob), 45000)
        self.assertEqual(decode(blob), records)

    def test_empty_strings_mixed(self):
        rng = random.Random(2)
        records = [["" if rng.random() < 0.5 else f"v{i}"] for i in range(500)]
        assert_roundtrip(self, records)

    def test_empty_string_vs_nonempty_same_column(self):
        records = [["", "x", "", "y", ""]]
        self.assertEqual(decode(encode(records)), records)

    def test_super_long_values(self):
        long_a = "".join(random.Random(3).choices(string.printable, k=200_000))
        long_b = "汉" * 100_000  # 多字节超长值
        records = [[long_a, "x"], [long_b, "x"], [long_a, "y"]]
        blob = assert_roundtrip(self, records)
        # long_a 重复出现，字典模式下应只存一份（按 utf-8 字节数比较）
        unique_bytes = len(long_a.encode()) + len(long_b.encode())
        naive_bytes = 2 * len(long_a.encode()) + len(long_b.encode())
        self.assertLess(len(blob), unique_bytes + 1000)
        self.assertLess(len(blob), naive_bytes)

    def test_very_many_columns(self):
        rng = random.Random(4)
        num_cols = 3000
        records = [[rng.choice("abc") for _ in range(num_cols)]
                   for _ in range(20)]
        blob, stats = encode_with_stats(records)
        self.assertEqual(stats.num_cols, num_cols)
        self.assertEqual(decode(blob), records)

    def test_all_unique_triggers_raw_fallback(self):
        records = [[f"unique-value-{i}-{'x' * 20}"] for i in range(5000)]
        blob, stats = encode_with_stats(records)
        self.assertEqual(stats.columns[0].mode, MODE_RAW_NAME)
        self.assertEqual(stats.columns[0].dict_size, 0)
        self.assertEqual(decode(blob), records)

    def test_unicode_and_control_chars(self):
        records = [["中文值", "emoji🎉", "tab\there", "newline\nhere", ""]]
        assert_roundtrip(self, records)

    def test_fuzz_random_distributions(self):
        rng = random.Random(5)
        for trial in range(50):
            num_rows = rng.randrange(0, 200)
            num_cols = rng.randrange(0, 10)
            cardinality = rng.choice([1, 2, 5, 1000])
            pool = ["".join(rng.choices("ab中 ", k=rng.randrange(0, 30)))
                    for _ in range(cardinality)]
            records = [[rng.choice(pool) for _ in range(num_cols)]
                       for _ in range(num_rows)]
            assert_roundtrip(self, records)


class ModeSelectionTest(unittest.TestCase):
    def test_choose_mode_dict_for_repetitive(self):
        self.assertEqual(choose_mode(["a", "b"] * 100), MODE_DICT_NAME)

    def test_choose_mode_raw_for_unique(self):
        self.assertEqual(choose_mode([f"v{i}" for i in range(100)]),
                         MODE_RAW_NAME)

    def test_choose_mode_empty(self):
        self.assertEqual(choose_mode([]), MODE_DICT_NAME)

    def test_dict_never_larger_than_raw_by_much(self):
        """无论选哪种模式，实际编码体积不应显著超过朴素存储。"""
        rng = random.Random(6)
        for cardinality in (1, 2, 10, 500, 5000):
            records = [[f"value-{rng.randrange(cardinality)}-padpadpad"]
                       for _ in range(5000)]
            blob, stats = encode_with_stats(records)
            # 允许估算口径误差，但实际字节数不得超出 raw 估算 +5%
            self.assertLessEqual(stats.columns[0].encoded_bytes,
                                 stats.columns[0].raw_bytes * 1.05 + 16,
                                 f"cardinality={cardinality} 时编码劣化")
            self.assertEqual(decode(blob), records)


class SelfContainedTest(unittest.TestCase):
    def test_decode_needs_only_bytes(self):
        records = [["s1", "r1"], ["s2", "r1"], ["s1", "r2"]]
        blob = encode(records)
        # 只拿字节流（模拟跨进程/落盘后）即可解码
        self.assertEqual(decode(bytes(blob)), records)

    def test_corrupt_magic_rejected(self):
        with self.assertRaises(ValueError):
            decode(b"XXXX" + encode([["a"]])[4:])

    def test_truncated_data_rejected(self):
        blob = encode([["hello", "world"], ["a", "b"]])
        with self.assertRaises(ValueError):
            decode(blob[:len(blob) // 2])

    def test_trailing_garbage_rejected(self):
        with self.assertRaises(ValueError):
            decode(encode([["a"]]) + b"junk")

    def test_ragged_rows_rejected(self):
        with self.assertRaises(ValueError):
            encode([["a", "b"], ["c"]])

    def test_non_string_rejected(self):
        with self.assertRaises(TypeError):
            encode([[1, 2]])


if __name__ == "__main__":
    unittest.main()
