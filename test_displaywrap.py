"""displaywrap 自测：字符类用例 / 折行 / 截断 / 制表符 / 禁拆片段 / 流式一致性。

运行：python3 -m unittest test_displaywrap -v
"""

import random
import tracemalloc
import unittest

from displaywrap import (
    Wrapper,
    char_width,
    cluster_width,
    clusters,
    display_width,
    expand_tabs,
    truncate,
    wrap,
)

ZWSP = "​"
ZWJ = "‍"
ZWNJ = "‌"
BOM = "﻿"
VS16 = "️"


class TestCharClasses(unittest.TestCase):
    """字符类用例清单：宽度与簇完整性。"""

    def test_ascii_and_latin(self):
        self.assertEqual(display_width("abc"), 3)
        self.assertEqual(char_width("a"), 1)

    def test_cjk_wide_and_fullwidth(self):
        self.assertEqual(char_width("中"), 2)
        self.assertEqual(char_width("Ａ"), 2)   # 全角拉丁
        self.assertEqual(char_width("，"), 2)   # 全角标点
        self.assertEqual(display_width("你好，世界"), 10)

    def test_combining_marks_zero_width(self):
        self.assertEqual(char_width("́"), 0)      # U+0301 Mn
        self.assertEqual(char_width("⃝"), 0)      # U+20DD Me
        self.assertEqual(clusters("é"), ["é"])  # 基字符+组合标记为一簇
        self.assertEqual(display_width("é"), 1)

    def test_zero_width_chars(self):
        for ch in (ZWSP, ZWJ, ZWNJ, BOM):
            self.assertEqual(char_width(ch), 0, hex(ord(ch)))
        self.assertEqual(display_width("a" + ZWSP + "b"), 2)

    def test_variation_selector(self):
        self.assertEqual(char_width(VS16), 0)
        self.assertEqual(clusters("❤️"), ["❤️"])
        self.assertEqual(display_width("❤️"), 2)   # emoji 表现形式
        self.assertEqual(display_width("❤"), 1)    # 文本表现形式

    def test_astral_emoji_not_split(self):
        # 代理对（星平面字符）在 Python 3 中是单码位，必须整体处理
        self.assertEqual(clusters("😀"), ["😀"])
        self.assertEqual(display_width("😀"), 2)

    def test_zwj_sequence_single_cluster(self):
        fam = "👨‍👩‍👧"
        self.assertEqual(clusters(fam), [fam])
        self.assertEqual(display_width(fam), 2)

    def test_flag_regional_indicators(self):
        flag = "🇨🇳"
        self.assertEqual(clusters(flag), [flag])
        self.assertEqual(display_width(flag), 2)

    def test_skin_tone_modifier(self):
        self.assertEqual(clusters("👍🏽"), ["👍🏽"])
        self.assertEqual(display_width("👍🏽"), 2)

    def test_keycap_sequence(self):
        self.assertEqual(clusters("1️⃣"), ["1️⃣"])
        self.assertEqual(display_width("1️⃣"), 2)

    def test_lone_surrogate(self):
        # 孤立代理项：不崩溃，按宽 1 独立簇处理
        self.assertEqual(clusters("\ud800"), ["\ud800"])
        self.assertEqual(display_width("\ud800x"), 2)

    def test_control_chars_zero_width(self):
        self.assertEqual(char_width("\x00"), 0)
        self.assertEqual(char_width("\t"), 0)  # display_width 不展开制表符


class TestWrap(unittest.TestCase):
    def test_basic_space_break(self):
        self.assertEqual(wrap("hello world foo", 11), ["hello world", "foo"])

    def test_empty_and_blank(self):
        self.assertEqual(wrap("", 5), [])
        self.assertEqual(wrap("   ", 5), [])

    def test_long_word_force_break(self):
        self.assertEqual(wrap("abcdefghij", 4), ["abcd", "efgh", "ij"])

    def test_cjk_wrap(self):
        self.assertEqual(wrap("你好世界", 4), ["你好", "世界"])
        self.assertEqual(wrap("你好世界", 5), ["你好", "世界"])

    def test_wide_char_at_boundary(self):
        self.assertEqual(wrap("ab你", 3), ["ab", "你"])
        self.assertEqual(wrap("ab你", 2), ["ab", "你"])

    def test_combining_cluster_not_split(self):
        lines = wrap("éx", 1)
        self.assertEqual(lines, ["é", "x"])

    def test_zwj_emoji_not_split(self):
        fam = "👨‍👩‍👧"
        self.assertEqual(wrap(fam + "x", 2), [fam, "x"])

    def test_zero_width_in_word(self):
        self.assertEqual(wrap("a" + ZWSP + "b", 2), ["a" + ZWSP + "b"])

    def test_single_cluster_wider_than_width(self):
        # 单簇（宽 2）超出行宽 1：整体放置，允许溢出
        self.assertEqual(wrap("你a", 1), ["你", "a"])

    def test_hard_newlines(self):
        self.assertEqual(wrap("a\n\nb", 5), ["a", "", "b"])
        self.assertEqual(wrap("a\n", 5), ["a"])

    def test_whitespace_rules(self):
        self.assertEqual(wrap("  hi  ", 10), ["hi"])       # 行首/行尾空白丢弃
        self.assertEqual(wrap("a \nb", 10), ["a", "b"])    # 硬换行前的空格丢弃
        self.assertEqual(wrap("a  b", 4), ["a  b"])        # 放得下则保留
        self.assertEqual(wrap("a  b", 3), ["a", "b"])      # 断行点空白丢弃

    def test_crlf(self):
        self.assertEqual(wrap("a\r\nb", 5), ["a", "b"])

    def test_mixed_script(self):
        self.assertEqual(wrap("hello 世界 foo", 8), ["hello", "世界 foo"])


class TestAtoms(unittest.TestCase):
    def test_atom_moves_as_whole(self):
        self.assertEqual(
            wrap("foo New York bar", 12, atoms=["New York"]),
            ["foo New York", "bar"],
        )
        self.assertEqual(
            wrap("foo New York bar", 10, atoms=["New York"]),
            ["foo", "New York", "bar"],
        )

    def test_atom_wider_than_width(self):
        # 禁拆片段自身超宽：独占一行，允许溢出，绝不切开
        self.assertEqual(
            wrap("x NewYorkAtom y", 4, atoms=["NewYorkAtom"]),
            ["x", "NewYorkAtom", "y"],
        )

    def test_atom_inside_word(self):
        lines = wrap("abNYcd", 3, atoms=["NY"])
        self.assertEqual(lines, ["ab", "NYc", "d"])
        self.assertIn("NY", lines[1])  # 禁拆片段完整

    def test_atom_with_emoji(self):
        self.assertEqual(
            wrap("a 🚀🚀 b", 3, atoms=["🚀🚀"]),
            ["a", "🚀🚀", "b"],
        )

    def test_atom_overlapping_longest_first(self):
        self.assertEqual(
            wrap("abcd", 2, atoms=["abc", "ab"]),
            ["abc", "d"],
        )

    def test_empty_atom_rejected(self):
        with self.assertRaises(ValueError):
            wrap("x", 5, atoms=[""])


class TestTabs(unittest.TestCase):
    def test_expand_default(self):
        self.assertEqual(wrap("a\tb", 10, tabsize=4), ["a   b"])
        self.assertEqual(expand_tabs("a\tb", 4), "a   b")

    def test_expand_resets_per_line(self):
        self.assertEqual(expand_tabs("ab\tc\nab\tc", 4), "ab  c\nab  c")

    def test_expand_uses_display_width(self):
        # 宽字符占 2 列，tab 对齐按显示列位
        self.assertEqual(expand_tabs("你\ta", 4), "你  a")

    def test_reject(self):
        with self.assertRaises(ValueError):
            wrap("a\tb", 10, tab="reject")
        with self.assertRaises(ValueError):
            truncate("a\tb", 10, tab="reject")

    def test_tab_width_consistency(self):
        # 展开后的空格参与折行，与直接写空格结果一致
        self.assertEqual(
            wrap("a\tb", 4, tabsize=4),
            wrap(expand_tabs("a\tb", 4), 4),
        )

    def test_leading_tab_dropped(self):
        self.assertEqual(wrap("\ta", 10, tabsize=4), ["a"])


class TestTruncate(unittest.TestCase):
    def test_fits_returns_as_is(self):
        self.assertEqual(truncate("hello", 5), "hello")
        self.assertEqual(truncate("你好", 4), "你好")

    def test_basic(self):
        self.assertEqual(truncate("hello", 4), "hel…")
        self.assertEqual(truncate("abcd", 3, ellipsis="..."), "...")

    def test_cjk(self):
        self.assertEqual(truncate("你好世界", 5), "你好…")
        self.assertEqual(truncate("你好世界", 4), "你…")

    def test_ellipsis_counts_in_width(self):
        self.assertEqual(display_width(truncate("你好世界", 5)), 5)

    def test_degrade_empty_string(self):
        # 降级规则：可用宽度 < 省略标记宽度 → 返回空串
        self.assertEqual(truncate("hello", 0), "")
        self.assertEqual(truncate("hello", 2, ellipsis="..."), "")

    def test_ellipsis_alone(self):
        self.assertEqual(truncate("hello", 1), "…")
        self.assertEqual(truncate("hello", 3, ellipsis="..."), "...")

    def test_cluster_not_split(self):
        self.assertEqual(truncate("éxy", 2), "é…")
        fam = "👨‍👩‍👧"
        self.assertEqual(truncate(fam + "ab", 3), fam + "…")
        self.assertEqual(truncate(fam, 2), fam)  # 恰好放下则不截断
        self.assertEqual(truncate(fam + "ab", 2), "…")  # 预算放不下宽簇

    def test_zero_width_kept(self):
        self.assertEqual(truncate("a" + ZWSP + "cdef", 3), "a" + ZWSP + "c…")

    def test_single_line_rule(self):
        self.assertEqual(truncate("abc\ndef", 10), "abc")

    def test_tab_expand(self):
        self.assertEqual(truncate("a\tb", 10, tabsize=4), "a   b")


class TestStreaming(unittest.TestCase):
    def _stream(self, text, sizes, **kw):
        w = Wrapper(**kw)
        out = []
        i = 0
        for s in sizes:
            out.extend(w.feed(text[i:i + s]))
            i += s
        out.extend(w.feed(text[i:]))
        out.extend(w.finish())
        return out

    def test_all_two_way_splits(self):
        text = "你好 abc👨‍👩‍👧 déf  New York\t尾\n下一行"
        kw = dict(width=7, tabsize=4, atoms=["New York"])
        expect = wrap(text, **kw)
        for i in range(len(text) + 1):
            got = self._stream(text, [i], **kw)
            self.assertEqual(got, expect, f"split at {i}")

    def test_all_three_way_splits_small(self):
        text = "a你👨‍👩‍👧 b\tc"
        kw = dict(width=4, tabsize=2, atoms=["👨‍👩‍👧"])
        expect = wrap(text, **kw)
        for i in range(len(text) + 1):
            for j in range(i, len(text) + 1):
                got = self._stream(text, [i, j - i], **kw)
                self.assertEqual(got, expect, f"splits {i},{j}")

    def test_char_by_char(self):
        text = "The quick 棕色🦊 fox jumps over the lazy 🐕 " * 3
        kw = dict(width=13)
        expect = wrap(text, **kw)
        got = self._stream(text, [1] * len(text), **kw)
        self.assertEqual(got, expect)

    def test_random_fuzz(self):
        rnd = random.Random(20260925)
        alphabet = [
            "a", "bc", " ", "  ", "\n", "\t", "你", "好", "，", "😀",
            "👨‍👩‍👧", "é", ZWSP, "🇨🇳", "1️⃣", "New York", "xylophone",
        ]
        atoms = ["New York", "👨‍👩‍👧", "不可拆"]
        for trial in range(300):
            text = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 120)))
            kw = dict(
                width=rnd.randint(1, 12),
                tabsize=rnd.choice([2, 4, 8]),
                atoms=atoms,
            )
            expect = wrap(text, **kw)
            sizes = []
            remaining = len(text)
            while remaining > 0:
                s = rnd.randint(1, 9)
                sizes.append(s)
                remaining -= s
            got = self._stream(text, sizes, **kw)
            self.assertEqual(got, expect, f"trial {trial}: {text!r}")

    def test_feed_after_finish_raises(self):
        w = Wrapper(5)
        w.finish()
        with self.assertRaises(ValueError):
            w.feed("x")

    def test_memory_bound(self):
        # 流式内部缓冲与输入总量无关：大输入下峰值内存有界
        rnd = random.Random(7)
        text = "".join(
            rnd.choice(["word ", "你好", "😀", "é", "\n"]) for _ in range(60000)
        )
        w = Wrapper(80, atoms=["New York"])
        tracemalloc.start()
        for i in range(0, len(text), 4096):
            w.feed(text[i:i + 4096])  # 丢弃输出行，模拟逐行消费
        w.finish()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024, f"peak={peak}")


if __name__ == "__main__":
    unittest.main()
