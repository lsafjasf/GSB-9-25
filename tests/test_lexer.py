"""Stage 1 unit tests: tokenisation (normal / boundary / malformed)."""

import unittest

from ql.errors import ErrorCode, ParseError
from ql.lexer import Token, tokenize


def kinds(tokens):
    return [(t.kind, t.value) for t in tokens]


class LexerNormalTests(unittest.TestCase):
    def test_full_query_tokens(self):
        toks = tokenize('SELECT a FROM t WHERE a = 1 LIMIT 5')
        self.assertEqual(kinds(toks), [
            ("KEYWORD", "SELECT"), ("IDENT", "a"),
            ("KEYWORD", "FROM"), ("IDENT", "t"),
            ("KEYWORD", "WHERE"), ("IDENT", "a"),
            ("OP", "="), ("NUMBER", 1),
            ("KEYWORD", "LIMIT"), ("NUMBER", 5),
        ])

    def test_keywords_are_case_insensitive_identifiers_preserved(self):
        toks = tokenize('select A fRoM TableX')
        self.assertEqual([t.value for t in toks
                          if t.kind == "KEYWORD"], ["SELECT", "FROM"])
        self.assertEqual(toks[1].value, "A")
        self.assertEqual(toks[3].value, "TableX")

    def test_returns_tuple_immutable(self):
        toks = tokenize("a 1")
        self.assertIsInstance(toks, tuple)
        self.assertEqual(hash(toks), hash(toks))
        with self.assertRaises(TypeError):
            toks[0] = None  # type: ignore[index]

    def test_offsets_point_at_first_character(self):
        toks = tokenize("  ab  12")
        self.assertEqual([t.offset for t in toks], [2, 6])
        self.assertEqual(toks[1].raw, "12")


class LexerBoundaryTests(unittest.TestCase):
    def test_empty_and_whitespace_only(self):
        self.assertEqual(tokenize(""), ())
        self.assertEqual(tokenize(" \t\r\n"), ())

    def test_comments_to_end_of_line_only(self):
        toks = tokenize("a -- ignored\nb -- x")
        self.assertEqual([t.value for t in toks], ["a", "b"])

    def test_minus_minus_inside_number_or_op(self):
        # "1--2" is number, comment to EOL; "-1" is OP minus + NUMBER.
        self.assertEqual(kinds(tokenize("1--2\n")), [("NUMBER", 1)])
        self.assertEqual(kinds(tokenize("-1")),
                         [("OP", "-"), ("NUMBER", 1)])

    def test_all_comparison_operators(self):
        self.assertEqual([t.value for t in tokenize("= <> != < <= > >=")],
                         ["=", "<>", "!=", "<", "<=", ">", ">="])

    def test_string_escapes_decoded(self):
        toks = tokenize(r'"a\n b\t c\" d\\"')
        self.assertEqual(toks[0].value, 'a\n b\t c" d\\')

    def test_identifier_boundaries(self):
        self.assertEqual([t.kind for t in tokenize("_a a1 _1")],
                         ["IDENT", "IDENT", "IDENT"])

    def test_multiline_string_is_not_allowed(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize('"a\nb"')
        self.assertEqual(ctx.exception.code, ErrorCode.UNCLOSED_STRING)
        self.assertEqual(ctx.exception.offset, 0)


class LexerMalformedTests(unittest.TestCase):
    def test_unexpected_characters_report_offset(self):
        for text, bad in [("a @b", "@"), ("#", "#"), ("a*b", "*"),
                          ("a+b", "+"), ("a.b", ".")]:
            with self.subTest(text=text):
                with self.assertRaises(ParseError) as ctx:
                    tokenize(text)
                self.assertEqual(ctx.exception.code,
                                 ErrorCode.UNEXPECTED_CHAR)
                self.assertEqual(text[ctx.exception.offset], bad)

    def test_unterminated_string(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize('a = "abc')
        self.assertEqual(ctx.exception.code, ErrorCode.UNCLOSED_STRING)
        self.assertEqual(ctx.exception.offset, 4)

    def test_unterminated_escape_at_eof(self):
        # single trailing backslash: malformed escape, not a close
        with self.assertRaises(ParseError) as ctx:
            tokenize('"abc\\')
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_ESCAPE)

    def test_bad_escape_offset_points_at_escape_char(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize(r'"\q"')
        err = ctx.exception
        self.assertEqual(err.code, ErrorCode.BAD_ESCAPE)
        self.assertEqual(err.offset, 2)  # 0=quote 1=backslash 2=q

    def test_bare_bang_is_illegal(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize("a ! b")
        self.assertEqual(ctx.exception.code, ErrorCode.UNEXPECTED_CHAR)


if __name__ == "__main__":
    unittest.main()
