"""test_regex.py — 引擎自测（unittest，标准库）。

运行: python3 test_regex.py -v
"""
import time
import unittest

import regex_engine as E
from regex_engine import RegexSyntaxError, search


class TestBasic(unittest.TestCase):
    def test_literal(self):
        self.assertEqual(search("abc", "xxabcyy"), (2, 5))
        self.assertIsNone(search("abc", "abd"))

    def test_empty_pattern(self):
        self.assertEqual(search("", "abc"), (0, 0))
        self.assertEqual(search("", ""), (0, 0))

    def test_empty_input(self):
        self.assertIsNone(search("a", ""))
        self.assertEqual(search("a*", ""), (0, 0))
        self.assertEqual(search("^$", ""), (0, 0))
        self.assertEqual(search("^", ""), (0, 0))
        self.assertEqual(search("$", ""), (0, 0))

    def test_dot(self):
        self.assertEqual(search("a.c", "abc"), (0, 3))
        self.assertIsNone(search("a.c", "a\nc"))  # 点号不匹配换行

    def test_quantifiers(self):
        self.assertEqual(search("ab*c", "abbbc"), (0, 5))
        self.assertEqual(search("ab*c", "ac"), (0, 2))
        self.assertEqual(search("ab+c", "abbbc"), (0, 5))
        self.assertIsNone(search("ab+c", "ac"))
        self.assertEqual(search("ab?c", "ac"), (0, 2))
        self.assertEqual(search("ab?c", "abc"), (0, 3))
        self.assertIsNone(search("ab?c", "abbc"))
        self.assertEqual(search("a*", "baa"), (0, 0))  # 左most 空匹配
        self.assertEqual(search("a*", "aaa"), (0, 3))  # 贪婪

    def test_leftmost(self):
        self.assertEqual(search("a+", "baaab"), (1, 4))
        self.assertEqual(search("b", "abc"), (1, 2))


class TestCharClass(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(search("[abc]", "zb"), (1, 2))
        self.assertIsNone(search("[abc]", "zz"))

    def test_range(self):
        self.assertEqual(search("[a-c]+", "zabcz"), (1, 4))
        self.assertEqual(search("[0-9]", "a5"), (1, 2))

    def test_negated(self):
        self.assertEqual(search("[^a]", "aab"), (2, 3))
        self.assertEqual(search("[^a]", "a\n"), (1, 2))  # 取反类可匹配换行

    def test_duplicate_ranges(self):
        # 重复/重叠区间合法，等价于并集
        self.assertEqual(search("[a-ca-cb-d]", "xd"), (1, 2))
        self.assertEqual(search("[a-zA-Z_]+", "  Ab_"), (2, 5))

    def test_reversed_range_is_error(self):
        with self.assertRaises(RegexSyntaxError) as ctx:
            search("[z-a]", "z")
        self.assertIn("反序", ctx.exception.reason)

    def test_dash_literal(self):
        self.assertEqual(search("[-a]", "-"), (0, 1))  # 首位 '-' 为字面量
        self.assertEqual(search("[a-]", "-"), (0, 1))  # 末位 '-' 为字面量
        self.assertEqual(search("[a-c-]", "-"), (0, 1))

    def test_rbracket_literal_first(self):
        self.assertEqual(search("[]]", "]"), (0, 1))
        self.assertEqual(search("[^]]", "]x"), (1, 2))

    def test_class_escapes(self):
        self.assertEqual(search("[\\d]+", "ab123"), (2, 5))
        self.assertEqual(search("[\\D]+", "12ab3"), (2, 4))
        self.assertEqual(search("[^\\d]", "5a"), (1, 2))
        self.assertEqual(search("[\\]]", "]"), (0, 1))

    def test_unterminated(self):
        with self.assertRaises(RegexSyntaxError) as ctx:
            search("[abc", "a")
        self.assertEqual(ctx.exception.pos, 0)
        self.assertIn("未闭合", ctx.exception.reason)


class TestEscape(unittest.TestCase):
    def test_punctuation(self):
        self.assertEqual(search("a\\.c", "a.c"), (0, 3))
        self.assertIsNone(search("a\\.c", "abc"))
        self.assertEqual(search("\\*\\+\\?\\[\\]\\^\\$\\\\", "*+?[]^$\\"), (0, 8))

    def test_classes(self):
        self.assertEqual(search("\\d+", "ab123"), (2, 5))
        self.assertEqual(search("\\D+", "12ab 3"), (2, 5))
        self.assertEqual(search("\\w+", "!a_1!"), (1, 4))
        self.assertEqual(search("\\W+", "ab !c"), (2, 4))
        self.assertEqual(search("\\s", "a b"), (1, 2))
        self.assertEqual(search("\\S+", " ab "), (1, 3))

    def test_dangling_escape(self):
        with self.assertRaises(RegexSyntaxError) as ctx:
            search("abc\\", "abc")
        self.assertEqual(ctx.exception.pos, 3)
        self.assertIn("悬空转义", ctx.exception.reason)

    def test_bad_escape(self):
        with self.assertRaises(RegexSyntaxError) as ctx:
            search("a\\e", "ae")
        self.assertIn("未知转义", ctx.exception.reason)

    def test_control_escapes(self):
        self.assertEqual(search("a\\nb", "a\nb"), (0, 3))
        self.assertEqual(search("a\\tb", "a\tb"), (0, 3))
        self.assertEqual(search("\\r\\f\\v\\a", "\r\f\v\a"), (0, 4))
        self.assertIsNone(search("\\n", "n"))  # 转义不等于字母本身

    def test_control_escapes_in_class(self):
        self.assertEqual(search("[\\n\\t]+", "a\n\tb"), (1, 3))
        self.assertEqual(search("[^\\n]", "\nx"), (1, 2))
        self.assertEqual(search("[\\b]", "\x08"), (0, 1))  # 类内 \b 为退格符

    def test_unicode_classes(self):
        # \d \w \s 与 re 一致按 Unicode 语义
        self.assertEqual(search("\\d+", "a٣٥b"), (1, 3))      # 阿拉伯-印度数字
        self.assertEqual(search("\\d", "４"), (0, 1))          # 全角数字
        self.assertIsNone(search("\\d", "²"))                  # No 类别不是 \d
        self.assertEqual(search("\\w+", " 中_é1 "), (1, 5))
        self.assertEqual(search("\\s", "a\xa0b"), (1, 2))      # 不换行空格
        self.assertEqual(search("\\s", "a\u3000b"), (1, 2))    # 全角空格

    def test_unicode_classes_negated(self):
        self.assertIsNone(search("\\D+", "٣٣"))                # 修复前误匹配
        self.assertIsNone(search("[^\\d]", "٣"))               # 修复前误匹配
        self.assertIsNone(search("\\W", "中"))
        self.assertIsNone(search("\\S", "\xa0"))
        self.assertEqual(search("\\D+", "٣ab٣"), (1, 3))
        self.assertEqual(search("[^\\w]+", "中!é"), (1, 2))


class TestAnchor(unittest.TestCase):
    def test_start(self):
        self.assertEqual(search("^abc", "abc"), (0, 3))
        self.assertIsNone(search("^abc", "xabc"))
        self.assertIsNone(search("^abc", "ab\nabc"))  # 无 MULTILINE

    def test_end(self):
        self.assertEqual(search("abc$", "abc"), (0, 3))
        self.assertIsNone(search("abc$", "abcx"))
        # 与 re 一致：$ 匹配结尾换行符之前
        self.assertEqual(search("abc$", "abc\n"), (0, 3))
        self.assertEqual(search("a$", "a\n"), (0, 1))
        self.assertIsNone(search("a$", "a\nb"))

    def test_combo(self):
        self.assertEqual(search("^$", ""), (0, 0))
        self.assertIsNone(search("^$", "a"))
        self.assertEqual(search("^a*$", "aaa"), (0, 3))
        self.assertIsNone(search("^a*$", "aab"))
        self.assertEqual(search("^$", "\n"), (0, 0))  # re: ^$ 匹配 "\n" 的 (0,0)

    def test_anchor_mid_pattern(self):
        self.assertIsNone(search("a^b", "ab"))
        self.assertIsNone(search("a$b", "ab"))


class TestSyntaxErrors(unittest.TestCase):
    def test_nothing_to_repeat(self):
        for p in ("*abc", "+", "?"):
            with self.assertRaises(RegexSyntaxError) as ctx:
                E.compile(p)
            self.assertEqual(ctx.exception.pos, 0)
            self.assertIn("缺少操作数", ctx.exception.reason)

    def test_multiple_repeat(self):
        with self.assertRaises(RegexSyntaxError) as ctx:
            E.compile("a**")
        self.assertEqual(ctx.exception.pos, 2)
        self.assertIn("连续量词", ctx.exception.reason)

    def test_error_has_position_and_reason(self):
        try:
            E.compile("ab[cd")
        except RegexSyntaxError as e:
            self.assertIsInstance(e.pos, int)
            self.assertTrue(e.reason)
            self.assertIn("position 2", str(e))
        else:
            self.fail("应当抛出 RegexSyntaxError")


class TestPathological(unittest.TestCase):
    def test_nested_star_effect(self):
        # 等价于 (a*)*b 的扁平形态：回溯法在这里是组合爆炸
        pat = "a*" * 20 + "b"
        text = "a" * 5000
        t0 = time.perf_counter()
        self.assertIsNone(search(pat, text))
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_question_marks(self):
        pat = "a?" * 30 + "a" * 30
        text = "a" * 30
        t0 = time.perf_counter()
        self.assertEqual(search(pat, text), (0, 30))
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_long_input(self):
        text = "ab" * 500_000  # 1MB
        t0 = time.perf_counter()
        self.assertEqual(search("[a-z]+[0-9]", text + "9"), (0, 1_000_001))
        self.assertLess(time.perf_counter() - t0, 10.0)


if __name__ == "__main__":
    unittest.main()
