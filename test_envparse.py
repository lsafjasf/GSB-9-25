"""envparse 回归测试：python3 -m unittest test_envparse -v"""

import unittest

from envparse import Entry, ParseError, parse_config, parse_line


def value_of(line, **kwargs):
    entry = parse_line(line, **kwargs)
    assert entry is not None
    return entry.value


class TestSingleQuotes(unittest.TestCase):
    def test_spaces_preserved(self):
        self.assertEqual(value_of("A='hello world'"), "hello world")

    def test_no_escape_inside_single_quotes(self):
        self.assertEqual(value_of(r"A='C:\new\tmp'"), r"C:\new\tmp")

    def test_double_quote_is_literal_inside(self):
        self.assertEqual(value_of("""A='say "hi"'"""), 'say "hi"')

    def test_backslash_before_closing_quote_is_literal(self):
        self.assertEqual(value_of("A='a\\'"), "a\\")

    def test_empty_single_quotes(self):
        self.assertEqual(value_of("A=''"), "")


class TestDoubleQuotes(unittest.TestCase):
    def test_spaces_preserved(self):
        self.assertEqual(value_of('A="hello world"'), "hello world")

    def test_supported_escape_sequences(self):
        self.assertEqual(value_of(r'A="a\nb\tc\rd"'), "a\nb\tc\rd")
        self.assertEqual(value_of(r'A="a\\b\"c\$d"'), 'a\\b"c$d')

    def test_unknown_escape_kept_verbatim(self):
        self.assertEqual(value_of(r'A="a\qb"'), r"a\qb")

    def test_single_quote_is_literal_inside(self):
        self.assertEqual(value_of("""A="it's ok\""""), "it's ok")

    def test_empty_double_quotes(self):
        self.assertEqual(value_of('A=""'), "")


class TestNestedQuotes(unittest.TestCase):
    def test_double_inside_single(self):
        self.assertEqual(value_of("""A='he said "hi" loudly'"""), 'he said "hi" loudly')

    def test_single_inside_double(self):
        self.assertEqual(value_of("""A="it's a 'test'" """), "it's a 'test'")

    def test_adjacent_quoted_segments_concatenate(self):
        self.assertEqual(value_of("""A='a'"b"c"""), "abc")


class TestComments(unittest.TestCase):
    def test_trailing_comment_stripped(self):
        self.assertEqual(value_of("A=value # a comment"), "value")

    def test_full_line_comment_skipped(self):
        self.assertIsNone(parse_line("# just a comment"))
        self.assertIsNone(parse_line("   # indented comment"))

    def test_hash_inside_quotes_kept(self):
        self.assertEqual(value_of("A='v1 # not a comment'"), "v1 # not a comment")
        self.assertEqual(value_of('A="v1 # not a comment"'), "v1 # not a comment")

    def test_hash_without_preceding_space_is_literal(self):
        self.assertEqual(value_of("A=v1#2"), "v1#2")

    def test_hash_at_value_start_is_comment(self):
        self.assertEqual(value_of("A=#comment"), "")

    def test_escaped_hash_is_literal(self):
        self.assertEqual(value_of(r"A=\#not-comment"), "#not-comment")


class TestEmptyVsMissing(unittest.TestCase):
    def test_empty_value(self):
        entry = parse_line("A=")
        self.assertTrue(entry.has_value)
        self.assertEqual(entry.value, "")

    def test_missing_value_raises_by_default(self):
        with self.assertRaises(ParseError) as ctx:
            parse_line("A")
        self.assertEqual(ctx.exception.kind, "missing_equals")

    def test_missing_value_with_bare_key_flag(self):
        entry = parse_line("A", allow_bare_key=True)
        self.assertFalse(entry.has_value)
        self.assertIsNone(entry.value)

    def test_caller_can_distinguish_and_apply_default(self):
        config = {}
        for line in ("A=", "B=1"):
            entry = parse_line(line)
            config[entry.key] = entry.value if entry.has_value else "default"
        self.assertEqual(config, {"A": "", "B": "1"})


class TestWhitespaceAndIntegrity(unittest.TestCase):
    def test_unquoted_inner_spaces_kept(self):
        self.assertEqual(value_of("A=hello world"), "hello world")

    def test_unquoted_surrounding_spaces_trimmed(self):
        self.assertEqual(value_of("A=   padded   "), "padded")

    def test_quoted_surrounding_spaces_kept(self):
        self.assertEqual(value_of("A='  padded  '"), "  padded  ")
        self.assertEqual(value_of('A="  padded  "'), "  padded  ")

    def test_equals_sign_in_value_kept(self):
        self.assertEqual(value_of("A=b=c=d"), "b=c=d")

    def test_separator_chars_in_value_kept(self):
        self.assertEqual(value_of("A=a,b;c:d|e"), "a,b;c:d|e")

    def test_key_surrounding_spaces_trimmed(self):
        entry = parse_line("  KEY  =v")
        self.assertEqual(entry.key, "KEY")

    def test_escaped_space_outside_quotes(self):
        self.assertEqual(value_of(r"A=a\ b"), "a b")

    def test_escaped_equals_outside_quotes(self):
        self.assertEqual(value_of(r"A=a\=b"), "a=b")

    def test_blank_lines_skipped(self):
        self.assertIsNone(parse_line(""))
        self.assertIsNone(parse_line("   "))


class TestErrors(unittest.TestCase):
    def test_missing_equals_reports_line_and_column(self):
        with self.assertRaises(ParseError) as ctx:
            parse_line("NO_EQUALS", line_no=3)
        exc = ctx.exception
        self.assertEqual(exc.kind, "missing_equals")
        self.assertEqual(exc.line, 3)
        self.assertEqual(exc.column, len("NO_EQUALS") + 1)

    def test_unterminated_quote_reports_opening_column(self):
        line = 'A="unclosed'
        with self.assertRaises(ParseError) as ctx:
            parse_line(line, line_no=2)
        exc = ctx.exception
        self.assertEqual(exc.kind, "unterminated_quote")
        self.assertEqual(exc.line, 2)
        self.assertEqual(exc.column, line.index('"') + 1)

    def test_unterminated_single_quote(self):
        with self.assertRaises(ParseError) as ctx:
            parse_line("A='unclosed")
        self.assertEqual(ctx.exception.kind, "unterminated_quote")
        self.assertEqual(ctx.exception.column, 3)

    def test_empty_key_reports_equals_column(self):
        with self.assertRaises(ParseError) as ctx:
            parse_line("   =oops", line_no=5)
        exc = ctx.exception
        self.assertEqual(exc.kind, "empty_key")
        self.assertEqual(exc.line, 5)
        self.assertEqual(exc.column, 4)

    def test_dangling_backslash(self):
        with self.assertRaises(ParseError) as ctx:
            parse_line("A=abc\\")
        self.assertEqual(ctx.exception.kind, "dangling_escape")

    def test_error_message_contains_position(self):
        try:
            parse_line("=oops", line_no=9)
        except ParseError as exc:
            self.assertIn("line 9", str(exc))
            self.assertIn("column 1", str(exc))
        else:
            self.fail("expected ParseError")


class TestParseConfig(unittest.TestCase):
    def test_multi_line_with_line_numbers(self):
        text = "A=1\n\n# comment\nB='x y'\n"
        entries = parse_config(text)
        self.assertEqual(
            [(e.key, e.value, e.line) for e in entries],
            [("A", "1", 1), ("B", "x y", 4)],
        )

    def test_error_carries_correct_line_number(self):
        with self.assertRaises(ParseError) as ctx:
            parse_config("A=1\nB=2\nBAD LINE\n")
        self.assertEqual(ctx.exception.line, 3)
        self.assertEqual(ctx.exception.kind, "missing_equals")


if __name__ == "__main__":
    unittest.main()
