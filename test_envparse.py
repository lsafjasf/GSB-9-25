"""复现测试 + 回归测试。

运行：python3 -m unittest test_envparse -v

BugReproductionTests：针对 buggy_envparse（旧版），稳定复现五类现网问题。
RegressionTests：针对 envparse（修复版），锁定修复后的明确规则。
"""

import unittest

import buggy_envparse
import envparse
from envparse import Entry, ParseError


class BugReproductionTests(unittest.TestCase):
    """五类缺陷的稳定复现（断言旧版的错误行为）。"""

    def test_bug1_quote_treated_as_plain_char_and_value_truncated(self):
        # 缺陷 1：引号被当普通字符，值在空格处被截断
        self.assertEqual(buggy_envparse.parse_line('A="hello world"'), ("A", '"hello'))

    def test_bug2_backslash_escape_lost(self):
        # 缺陷 2：反斜杠被删除，\n 变成普通 n
        self.assertEqual(buggy_envparse.parse_line(r'A="a\nb"'), ("A", '"anb"'))

    def test_bug3_empty_value_indistinguishable_from_missing(self):
        # 缺陷 3：KEY= 与 KEY 都得到 ""，调用方无法区分空值与值缺失
        self.assertEqual(buggy_envparse.parse_line("A="), ("A", ""))
        self.assertEqual(buggy_envparse.parse_line("A"), ("A", ""))

    def test_bug4_comment_char_inside_quotes_truncates(self):
        # 缺陷 4：引号内的 # 被当作注释起始
        self.assertEqual(buggy_envparse.parse_line('A="a#b"'), ("A", '"a'))

    def test_bug5_invalid_line_error_has_no_position(self):
        # 缺陷 5：非法行只报 "parse failed"，无行号与列位置
        with self.assertRaises(ValueError) as ctx:
            buggy_envparse.parse_text("OK=1\n=bad\n")
        self.assertEqual(str(ctx.exception), "parse failed")
        self.assertFalse(hasattr(ctx.exception, "line"))
        self.assertFalse(hasattr(ctx.exception, "col"))


class QuoteRuleTests(unittest.TestCase):
    """引号规则：单引号、双引号、嵌套引号。"""

    def test_single_quotes_preserve_spaces(self):
        self.assertEqual(envparse.parse_line("A='hello world'").value, "hello world")

    def test_single_quotes_no_escape(self):
        # 单引号内不做转义：\n 是两个普通字符
        self.assertEqual(envparse.parse_line(r"A='a\nb'").value, r"a\nb")

    def test_double_quotes_preserve_spaces(self):
        self.assertEqual(envparse.parse_line('A="hello world"').value, "hello world")

    def test_double_quotes_escape_sequences(self):
        entry = envparse.parse_line(r'A="a\nb\tc\\d\"e\rf"')
        self.assertEqual(entry.value, 'a\nb\tc\\d"e\rf')

    def test_double_quotes_unknown_escape_kept_literally(self):
        self.assertEqual(envparse.parse_line(r'A="a\qb"').value, r"a\qb")

    def test_nested_single_inside_double(self):
        self.assertEqual(envparse.parse_line("A=\"it's ok\"").value, "it's ok")

    def test_nested_double_inside_single(self):
        self.assertEqual(envparse.parse_line("""A='say "hi"'""").value, 'say "hi"')

    def test_quoted_and_literal_segments_concatenate(self):
        self.assertEqual(envparse.parse_line('A=pre"middle"post').value, "premiddlepost")

    def test_backslash_outside_quotes_is_literal(self):
        self.assertEqual(envparse.parse_line(r"A=a\nb").value, r"a\nb")


class CommentTests(unittest.TestCase):
    """行尾注释与 # 的位置规则。"""

    def test_full_line_comment_skipped(self):
        self.assertIsNone(envparse.parse_line("# comment"))
        self.assertIsNone(envparse.parse_line("   # comment"))

    def test_trailing_comment_after_whitespace(self):
        self.assertEqual(envparse.parse_line("A=1  # note").value, "1  ")

    def test_hash_inside_quotes_is_literal(self):
        self.assertEqual(envparse.parse_line('A="a#b"').value, "a#b")
        self.assertEqual(envparse.parse_line("A='a#b'").value, "a#b")

    def test_hash_without_preceding_whitespace_is_literal(self):
        self.assertEqual(envparse.parse_line("A=a#b").value, "a#b")


class ValueIntegrityTests(unittest.TestCase):
    """值除引号与转义语义外不得被改动。"""

    def test_leading_and_trailing_spaces_preserved(self):
        self.assertEqual(envparse.parse_line("A=  spaced  ").value, "  spaced  ")

    def test_inner_separators_preserved(self):
        self.assertEqual(envparse.parse_line("A=a,b;c:d").value, "a,b;c:d")

    def test_equals_sign_in_value_preserved(self):
        self.assertEqual(envparse.parse_line("A=k1=v1&k2=v2").value, "k1=v1&k2=v2")

    def test_empty_value(self):
        entry = envparse.parse_line("A=")
        self.assertEqual(entry.value, "")
        self.assertTrue(entry.has_value)

    def test_quoted_empty_value(self):
        self.assertEqual(envparse.parse_line('A=""').value, "")


class EmptyVsMissingTests(unittest.TestCase):
    """空值与值缺失可区分，调用方能据此选择默认值。"""

    def test_empty_value_vs_missing_key(self):
        cfg = envparse.parse_text("EMPTY=\n")
        self.assertEqual(cfg.get_entry("EMPTY"), Entry("EMPTY", "", True))
        self.assertIsNone(cfg.get_entry("ABSENT"))
        self.assertEqual(cfg.get("EMPTY", "default"), "")       # 空值：不用默认值
        self.assertEqual(cfg.get("ABSENT", "default"), "default")  # 缺失：用默认值

    def test_missing_equals_is_error_not_empty_value(self):
        with self.assertRaises(ParseError):
            envparse.parse_line("A")


class InvalidLineTests(unittest.TestCase):
    """非法行报错并指出行号与列位置。"""

    def test_missing_equals_reports_position(self):
        with self.assertRaises(ParseError) as ctx:
            envparse.parse_text("OK=1\nBADLINE\n")
        err = ctx.exception
        self.assertEqual((err.line, err.col), (2, len("BADLINE") + 1))
        self.assertIn("missing '='", str(err))

    def test_empty_key_reports_position(self):
        with self.assertRaises(ParseError) as ctx:
            envparse.parse_text("=value\n")
        err = ctx.exception
        self.assertEqual((err.line, err.col), (1, 1))
        self.assertIn("empty key", str(err))

    def test_unterminated_single_quote_reports_opening_quote_column(self):
        line = "A='oops"
        with self.assertRaises(ParseError) as ctx:
            envparse.parse_line(line, lineno=7)
        err = ctx.exception
        self.assertEqual((err.line, err.col), (7, line.index("'") + 1))
        self.assertIn("unterminated single quote", str(err))

    def test_unterminated_double_quote_reports_opening_quote_column(self):
        line = 'A="oops'
        with self.assertRaises(ParseError) as ctx:
            envparse.parse_line(line)
        err = ctx.exception
        self.assertEqual((err.line, err.col), (1, line.index('"') + 1))
        self.assertIn("unterminated double quote", str(err))


class MultiLineTests(unittest.TestCase):
    def test_parse_text_skips_blank_and_comment_lines(self):
        cfg = envparse.parse_text(
            "# header\n"
            "\n"
            "A=1\n"
            "   \n"
            "B='x y'  # tail\n"
            "A=2\n"  # 重复键：后者覆盖前者
        )
        self.assertEqual(list(cfg.keys()), ["A", "B"])
        self.assertEqual(cfg.get("A"), "2")
        self.assertEqual(cfg.get("B"), "x y  ")


if __name__ == "__main__":
    unittest.main()
