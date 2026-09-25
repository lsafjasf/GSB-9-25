"""复现 + 回归测试。

用法：
    python3 test_table_align.py                          # 测修复后的 table_align（应全过）
    TABLE_MODULE=table_align_buggy python3 test_table_align.py   # 测修复前版本（5 类缺陷稳定复现）

每个用例对“每列起始位置”做精确断言：期望值由人工按终端显示宽度算出，
与被测实现无关。
"""

import importlib
import os
import unicodedata
import unittest

mod = importlib.import_module(os.environ.get("TABLE_MODULE", "table_align"))

PADDING = 2  # 与实现约定的列间距


def oracle_width(text):
    """测试侧独立的显示宽度 oracle（与被测实现无关，作为测试基准）。"""
    col = 0
    prev_zwj = False
    chars = list(text)
    for i, ch in enumerate(chars):
        cp = ord(ch)
        if ch == "\t":
            col += 8 - col % 8
        elif ch == "\u200d":
            prev_zwj = True
        elif (
            unicodedata.combining(ch)
            or unicodedata.category(ch) in ("Mn", "Me", "Cf", "Cc")
            or 0xFE00 <= cp <= 0xFE0F
            or 0x1F3FB <= cp <= 0x1F3FF
        ):
            pass
        elif prev_zwj:
            prev_zwj = False  # ZWJ 序列的后续字符不再占宽度
        elif i + 1 < len(chars) and chars[i + 1] == "\ufe0f":
            col += 2  # emoji 表现序列
        elif 0x1F1E6 <= cp <= 0x1F1FF:
            col += 2  # 国旗（区域指示符对）
        else:
            col += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return col


def cell_starts_at(line, col_start, cell):
    """该行的第 col_start 个显示列处确实是 cell 内容的开头（几何断言）。

    col_start 是终端显示列，不是 Python 字符串下标；
    用 oracle_width 把显示列换算成该行内的字符串下标。
    """
    for idx in range(len(line) + 1):
        w = oracle_width(line[:idx])
        if w == col_start:
            return line[idx:].startswith(cell)
        if w > col_start:
            return False
    return False


class ReproduceMisalignment(unittest.TestCase):
    """五类错位缺陷的复现用例（修复前全部失败，修复后全部通过）。"""

    def test_1_wide_cjk_chars(self):
        # “姓名”/“李雷”显示宽度为 4，len() 会算成 2，导致含中文行后面整列右移。
        rows = [
            ["姓名", "年龄"],
            ["Alice", "30"],
            ["李雷", "28"],
        ]
        lines = mod.render_table(rows).split("\n")
        col1_start = 5 + PADDING  # col0 显示宽度 max(4,5,4)=5
        self.assertTrue(cell_starts_at(lines[0], col1_start, "年龄"), lines)
        self.assertTrue(cell_starts_at(lines[1], col1_start, "30"), lines)
        self.assertTrue(cell_starts_at(lines[2], col1_start, "28"), lines)

    def test_2_combining_sequence(self):
        # "cafe\u0301"（e + 组合重音符）显示宽度 4，len() 算成 5。
        rows = [
            ["cafe\u0301", "x"],
            ["cafe", "y"],
        ]
        lines = mod.render_table(rows).split("\n")
        col1_start = 4 + PADDING
        self.assertTrue(cell_starts_at(lines[0], col1_start, "x"), lines)
        self.assertTrue(cell_starts_at(lines[1], col1_start, "y"), lines)

    def test_3_emoji_sequences(self):
        # 👍🏽 = 拇指 + 肤色修饰符，显示宽度 2；👨‍👩‍👧 = ZWJ 序列，显示宽度 2；
        # ❤️ = U+2764 + VS16，显示宽度 2。len() 分别算成 2/5/2（码点），全错。
        rows = [
            ["👍🏽", "a"],
            ["👨\u200d👩\u200d👧", "b"],
            ["❤️", "c"],
            ["ab", "d"],
        ]
        lines = mod.render_table(rows).split("\n")
        col1_start = 2 + PADDING  # col0 显示宽度均为 2
        for line, cell in zip(lines, "abcd"):
            self.assertTrue(cell_starts_at(line, col1_start, cell), lines)

    def test_4_zero_width_chars(self):
        # 零宽字符（ZWSP/ZWNJ）显示宽度 0，len() 算成 1，把后续列顶歪。
        rows = [
            ["ab\u200bc", "1"],   # 含 ZWSP，显示宽度 3
            ["a\u200cbc", "2"],   # 含 ZWNJ，显示宽度 3
            ["abc", "3"],
        ]
        lines = mod.render_table(rows).split("\n")
        col1_start = 3 + PADDING
        for line, cell in zip(lines, "123"):
            self.assertTrue(cell_starts_at(line, col1_start, cell), lines)

    def test_5_tabs_expanded_to_stops(self):
        # 制表符必须按列位展开（8 列一个制表位）："a\tb" 显示宽度 = 1+7+1 = 9。
        rows = [
            ["a\tb", "x"],
            ["abcd", "y"],
        ]
        lines = mod.render_table(rows).split("\n")
        col1_start = 9 + PADDING
        self.assertTrue(cell_starts_at(lines[0], col1_start, "x"), lines)
        self.assertTrue(cell_starts_at(lines[1], col1_start, "y"), lines)
        self.assertNotIn("\t", "\n".join(lines))  # 输出中不得残留制表符


class RegressionUnbreakableSequences(unittest.TestCase):
    """截断 / 折行不得拆散不可拆序列；宽度计算只有一个来源。"""

    def test_truncate_never_splits_cluster(self):
        family = "👨\u200d👩\u200d👧\u200d👦"  # 一家四口，显示宽度 2，共 7 个码点
        out = mod.truncate(family + "ab", 3)
        # 宽度 3 只能放下 family(2) + 'a'(1)；绝不允许切出半个 ZWJ 序列
        self.assertEqual(out, family + "a")
        self.assertEqual(mod.truncate(family, 1), "")  # 放不下就整簇舍弃

    def test_wrap_never_splits_cluster(self):
        text = "ab👨\u200d👩\u200d👧cd"
        lines = mod.wrap(text, 4)
        # ZWJ 不得出现在任何一行的首尾（即序列未被从中间切开）
        for line in lines:
            self.assertFalse(line.startswith("\u200d") or line.endswith("\u200d"), lines)
        # 每一行显示宽度 <= 4，且拼接回去内容不丢
        self.assertEqual("".join(lines), text)
        self.assertTrue(all(mod.display_width(l) <= 4 for l in lines))

    def test_display_width_values(self):
        w = mod.display_width
        self.assertEqual(w("中文"), 4)
        self.assertEqual(w("cafe\u0301"), 4)
        self.assertEqual(w("👨\u200d👩\u200d👧"), 2)
        self.assertEqual(w("👍🏽"), 2)
        self.assertEqual(w("❤️"), 2)
        self.assertEqual(w("🇨🇳"), 2)
        self.assertEqual(w("a\u200bb"), 2)
        self.assertEqual(w("a\tb"), 9)   # 制表符按 8 列位展开
        self.assertEqual(w("plain"), 5)

    def test_all_rows_column_starts_consistent(self):
        # 混合五类数据，逐列断言所有行的起始位置一致。
        rows = [
            ["姓名", "cafe\u0301", "👍🏽", "a\u200bb", "x\ty"],
            ["Alice", "cafe", "👨\u200d👩\u200d👧", "abc", "ok"],
            ["李雷", "naïve", "❤️", "ab", "z"],
        ]
        lines = mod.render_table(rows).split("\n")
        # col 显示宽度: c0=max(4,5,4)=5, c1=max(4,4,5)=5, c2=2, c3=max(2,3,2)=3
        starts = [0, 5 + PADDING, 5 + PADDING + 5 + PADDING,
                  5 + PADDING + 5 + PADDING + 2 + PADDING,
                  5 + PADDING + 5 + PADDING + 2 + PADDING + 3 + PADDING]
        # 渲染后制表符已展开，期望值同步展开（"x\ty" -> "x" + 7 空格 + "y"）
        expected_rows = [
            ["姓名", "cafe\u0301", "👍🏽", "a\u200bb", "x" + " " * 7 + "y"],
            ["Alice", "cafe", "👨\u200d👩\u200d👧", "abc", "ok"],
            ["李雷", "naïve", "❤️", "ab", "z"],
        ]
        for line, row in zip(lines, expected_rows):
            for col, (start, cell) in enumerate(zip(starts, row)):
                with self.subTest(line=line, col=col):
                    self.assertTrue(cell_starts_at(line, start, cell),
                                    (line, col, start))

    def test_tab_policy_consistent_across_pipeline(self):
        # 对齐、截断、折行对制表符的处理必须一致（一律按 8 列位展开）。
        self.assertEqual(mod.display_width("\t"), 8)
        self.assertEqual(mod.truncate("a\tb", 5), "a    ")  # 展开后按宽度截断
        self.assertEqual("".join(mod.wrap("a\tb", 20)), "a" + " " * 7 + "b")
        self.assertNotIn("\t", mod.render_table([["\t", "x"]]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
