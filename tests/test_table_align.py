"""复现测试 + 回归测试。

运行：python3 -m unittest discover -s tests -v
- BuggyReproTest：针对 src/table_align_buggy.py，稳定复现五类错位（断言缺陷存在）。
- FixedAlignTest：针对修复版，逐列精确断言每行的列起始位置（显示列）。
- RegressionTest：混合数据集上逐列断言所有行起始位置一致，并覆盖截断/折行。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import table_align as fixed
import table_align_buggy as buggy

COMBINING_E = "é"   # 'e' + U+0301，显示宽度 1
FAMILY = "👨‍👩‍👧"        # ZWJ 序列，不可拆
ZWSP_TEXT = "ab​cd"  # 含零宽空格 U+200B，显示宽度 4
EMOJI = "🙂"               # 宽度 2

# 覆盖五类问题的数据集：每行两列，第二列内容互不相同以便定位
DATASET = [
    ["abcd", "c1"],
    ["中文", "c2"],            # 宽字符
    [COMBINING_E, "c3"],       # 组合序列
    [EMOJI, "c4"],             # 表情符号
    [ZWSP_TEXT, "c5"],         # 零宽字符
    ["a\tb", "c6"],            # 制表符
]


def col2_start(line, cell):
    """第二列单元格在行内的起始“显示列”（用修复版的尺子量）。"""
    return fixed.display_width(line[: line.index(cell)])


class BuggyReproTest(unittest.TestCase):
    """稳定复现现网五类错位：缺陷版把 len() 当显示宽度。"""

    def test_repro_wide_char_shifts_row(self):
        lines = buggy.render([["abcd", "c1"], ["中文", "c2"]])
        # len("中文")==2，被当成宽度 2（实际 4），第二列提前 2 列
        self.assertNotEqual(col2_start(lines[0], "c1"), col2_start(lines[1], "c2"))

    def test_repro_combining_and_emoji_width(self):
        lines = buggy.render([["abcd", "c1"], [COMBINING_E, "c3"], [EMOJI, "c4"]])
        starts = [col2_start(l, c) for l, c in zip(lines, ["c1", "c3", "c4"])]
        self.assertNotEqual(starts[0], starts[1])  # 组合字符被计 2
        self.assertNotEqual(starts[0], starts[2])  # emoji 被计 1

    def test_repro_tab_pushes_columns(self):
        lines = buggy.render([["abcd", "c1"], ["a\tb", "c6"]])
        self.assertNotEqual(col2_start(lines[0], "c1"), col2_start(lines[1], "c6"))

    def test_repro_zero_width_shifts_columns(self):
        lines = buggy.render([["abcd", "c1"], [ZWSP_TEXT, "c5"]])
        self.assertNotEqual(col2_start(lines[0], "c1"), col2_start(lines[1], "c5"))

    def test_repro_truncate_splits_sequence(self):
        self.assertEqual(buggy.truncate(COMBINING_E, 1), "e")          # 丢了 U+0301
        cut = buggy.truncate(FAMILY, 3)
        self.assertNotEqual(cut, FAMILY)
        self.assertIn("‍", cut)                       # 留下半个 ZWJ 序列


class FixedAlignTest(unittest.TestCase):
    """修复版：逐列精确断言起始位置。"""

    def test_column_starts_exact(self):
        lines = fixed.render(DATASET)
        # 第一列显示宽度：abcd=4, 中文=4, é=1, 🙂=2, ab​cd=4, "a\tb"->9
        # 列宽 = 9，列间距 PAD=2，第二列统一起始于显示列 11
        for line, (_, cell) in zip(lines, DATASET):
            self.assertEqual(col2_start(line, cell), 11, msg=repr(line))

    def test_exact_output(self):
        lines = fixed.render(DATASET)
        self.assertEqual(lines, [
            "abcd" + " " * 7 + "c1",
            "中文" + " " * 7 + "c2",
            COMBINING_E + " " * 10 + "c3",
            EMOJI + " " * 9 + "c4",
            ZWSP_TEXT + " " * 7 + "c5",
            "a" + " " * 7 + "b" + " " * 2 + "c6",  # 制表符展开到列位 8
        ])

    def test_width_rules(self):
        self.assertEqual(fixed.display_width("中文"), 4)
        self.assertEqual(fixed.display_width(COMBINING_E), 1)
        self.assertEqual(fixed.display_width(EMOJI), 2)
        self.assertEqual(fixed.display_width(ZWSP_TEXT), 4)
        self.assertEqual(fixed.display_width("a\tb"), 9)   # a + 7 空格 + b
        self.assertEqual(fixed.display_width("ab\tb"), 9)  # ab + 6 空格 + b

    def test_tab_policy_consistent(self):
        # 全流程一致：对齐、截断、折行都先按列位展开
        self.assertNotIn("\t", fixed.render([["a\tb", "x"]])[0])
        self.assertNotIn("\t", fixed.truncate("a\tb", 8))
        self.assertNotIn("\t", "".join(fixed.wrap("a\tb", 8)))

    def test_truncate_never_splits_cluster(self):
        self.assertEqual(fixed.truncate(COMBINING_E, 1), COMBINING_E)
        self.assertEqual(fixed.truncate(FAMILY, 100), FAMILY)
        # 宽度不够放下整个 ZWJ 序列时整体舍弃，不留半个
        for n in range(0, fixed.display_width(FAMILY)):
            cut = fixed.truncate(FAMILY, n)
            self.assertNotIn("‍", cut)
        self.assertEqual(fixed.truncate("ab" + COMBINING_E + "cd", 3), "ab" + COMBINING_E)

    def test_wrap_uses_same_width(self):
        self.assertEqual(fixed.wrap("中文中文", 4), ["中文", "中文"])
        self.assertEqual(fixed.wrap("a" + COMBINING_E + "bc", 3), ["a" + COMBINING_E + "b", "c"])


class RegressionTest(unittest.TestCase):
    """回归：混合数据集上所有行的列起始位置逐列一致。"""

    def test_all_rows_aligned(self):
        rows = DATASET + [
            ["x", "c7"],
            ["👨‍👩‍👧", "c8"],
            ["宽char混排", "c9"],
        ]
        lines = fixed.render(rows)
        starts = [col2_start(line, cell) for line, (_, cell) in zip(lines, rows)]
        self.assertEqual(len(set(starts)), 1, msg=str(list(zip(rows, starts))))

    def test_single_width_source(self):
        # 对齐/截断/折行复用同一 display_width：宽度 w 的文本渲染后第二列必在 w+PAD
        for cell, marker in DATASET:
            w = fixed.display_width(cell)
            line = fixed.render([[cell, marker], ["", ""]])[0]
            self.assertEqual(col2_start(line, marker), w + fixed.PAD)


if __name__ == "__main__":
    unittest.main()
