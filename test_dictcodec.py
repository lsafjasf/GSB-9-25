"""dictcodec 自测：边界输入 + 与逐列朴素存储的逐格对拍。

运行：python3 -m unittest test_dictcodec -v
"""

import random
import string
import unittest

import dictcodec as dc


def random_value(rng, max_len=30):
    alphabet = string.ascii_letters + string.digits + " 中文字符🙂\t\n"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, max_len)))


def random_rows(rng, n_rows, n_cols, distinct_ratio):
    """distinct_ratio 控制每列去重值占比：0.01 高重复，1.0 几乎全不同。"""
    rows = []
    pools = []
    for _ in range(n_cols):
        k = max(1, int(n_rows * distinct_ratio))
        pools.append([random_value(rng) for _ in range(k)])
    for _ in range(n_rows):
        rows.append([rng.choice(pool) for pool in pools])
    return rows


class RoundTripTest(unittest.TestCase):
    def check(self, rows):
        blob = dc.encode(rows)
        self.assertEqual(dc.decode(blob), rows)

    def test_empty_table(self):
        self.check([])

    def test_zero_columns(self):
        self.check([[], [], []])

    def test_single_cell(self):
        self.check([["only"]])

    def test_single_value_column(self):
        self.check([["same"] for _ in range(1000)])

    def test_empty_strings(self):
        rows = [["", "x", ""], ["", "", "y"], ["z", "", ""]]
        self.check(rows)

    def test_all_empty_strings(self):
        self.check([[""] * 5 for _ in range(100)])

    def test_high_repetition(self):
        rng = random.Random(7)
        self.check(random_rows(rng, 5000, 6, 0.001))

    def test_all_distinct(self):
        rows = [[f"uuid-{i:08x}-{i * 31:08x}"] for i in range(2000)]
        self.check(rows)

    def test_very_long_values(self):
        long_val = "长" * 100_000 + "x" * 100_000
        rows = [[long_val, "a"], [long_val, "b"], ["short", "a"]]
        self.check(rows)

    def test_many_columns(self):
        rng = random.Random(11)
        rows = random_rows(rng, 20, 3000, 0.5)
        self.check(rows)

    def test_unicode_and_control_chars(self):
        rows = [["中文字段", "emoji🙂🎉", "tab\there", "newline\nhere", "\x00null"]]
        self.check(rows)

    def test_non_str_cell_rejected(self):
        with self.assertRaises(TypeError):
            dc.encode([[1]])
        with self.assertRaises(TypeError):
            dc.encode([[None]])

    def test_ragged_rows_rejected(self):
        with self.assertRaises(ValueError):
            dc.encode([["a"], ["a", "b"]])


class ModeSelectionTest(unittest.TestCase):
    def modes(self, rows):
        _, stats = dc.encode_with_stats(rows)
        return [c.mode for c in stats.columns]

    def test_repetitive_column_uses_dict(self):
        rows = [[v] for v in ["open", "closed"] * 500]
        self.assertEqual(self.modes(rows), ["dict"])

    def test_distinct_column_falls_back_to_raw(self):
        rows = [[f"unique-value-{i}"] for i in range(1000)]
        self.assertEqual(self.modes(rows), ["raw"])

    def test_empty_table_has_no_columns(self):
        blob, stats = dc.encode_with_stats([])
        self.assertEqual(stats.columns, [])
        self.assertEqual(dc.decode(blob), [])

    def test_single_value_column_uses_dict(self):
        rows = [["constant"] for _ in range(100)]
        _, stats = dc.encode_with_stats(rows)
        self.assertEqual(stats.columns[0].mode, "dict")
        self.assertEqual(stats.columns[0].distinct, 1)

    def test_chosen_mode_never_larger_than_raw(self):
        rng = random.Random(23)
        for ratio in (0.001, 0.1, 0.5, 0.9, 1.0):
            rows = random_rows(rng, 500, 4, ratio)
            _, stats = dc.encode_with_stats(rows)
            for col in stats.columns:
                self.assertLessEqual(col.encoded_cost, col.raw_cost)


class SelfContainedTest(unittest.TestCase):
    def test_blob_decodes_without_external_state(self):
        rows = [["alpha", "1"], ["beta", "2"], ["alpha", "3"]]
        blob = dc.encode(rows)
        # 模拟“单独拿到编码结果”：只凭 bytes 即可解码
        self.assertEqual(dc.decode(bytes(blob)), rows)

    def test_corruption_detected(self):
        blob = bytearray(dc.encode([["hello", "world"], ["foo", "bar"]]))
        with self.assertRaises(dc.DecodeError):
            dc.decode(b"NOPE" + bytes(blob[4:]))
        bad = bytearray(blob)
        bad[-1] ^= 0xFF
        with self.assertRaises((dc.DecodeError, UnicodeDecodeError)):
            dc.decode(bytes(bad))
        with self.assertRaises(dc.DecodeError):
            dc.decode(bytes(blob[: len(blob) // 2]))


class DifferentialTest(unittest.TestCase):
    """与逐列朴素存储对拍：两种编码解码后必须逐格一致，且都等于原表。"""

    def assert_cells_equal(self, a, b):
        self.assertEqual(len(a), len(b))
        for row_a, row_b in zip(a, b):
            self.assertEqual(len(row_a), len(row_b))
            for cell_a, cell_b in zip(row_a, row_b):
                self.assertEqual(cell_a, cell_b)

    def test_differential_fuzz(self):
        rng = random.Random(20260925)
        cases = [
            (0, 0, 0.5),      # 空表
            (1, 1, 1.0),      # 单格
            (300, 1, 0.01),   # 单列高重复
            (300, 1, 1.0),    # 单列全不同
            (200, 8, 0.1),
            (200, 8, 0.9),
            (10, 500, 0.3),   # 列数多
            (1000, 3, 0.001),
        ]
        for n_rows, n_cols, ratio in cases:
            rows = random_rows(rng, n_rows, n_cols, ratio)
            via_dict = dc.decode(dc.encode(rows))
            via_naive = dc.naive_decode(dc.naive_encode(rows))
            self.assert_cells_equal(via_dict, rows)
            self.assert_cells_equal(via_naive, rows)
            self.assert_cells_equal(via_dict, via_naive)

    def test_differential_with_nasty_values(self):
        rng = random.Random(99)
        rows = []
        for i in range(500):
            rows.append([
                "",                                  # 空字符串
                "x" * rng.randint(0, 5000),          # 超长取值
                random_value(rng, 5) if i % 3 else "常量",
                str(i),                              # 全不同
            ])
        via_dict = dc.decode(dc.encode(rows))
        via_naive = dc.naive_decode(dc.naive_encode(rows))
        self.assert_cells_equal(via_dict, rows)
        self.assert_cells_equal(via_dict, via_naive)


if __name__ == "__main__":
    unittest.main()
