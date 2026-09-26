"""Stage 2 unit tests: structure determination (tokens -> CST).

The parser never sees source text, so tests feed token tuples directly and
inspect immutable CST nodes.
"""

import unittest

from ql import cst
from ql.errors import ErrorCode, ParseError
from ql.lexer import tokenize
from ql.parser import parse


def prog(text):
    return parse(tokenize(text), len(text))


class ParserNormalTests(unittest.TestCase):
    def test_minimal_program(self):
        p = prog("SELECT a FROM t")
        self.assertEqual([c.name for c in p.select], ["a"])
        self.assertEqual(p.table.name, "t")
        self.assertIsNone(p.where)
        self.assertIsNone(p.limit)

    def test_columns_and_limit(self):
        p = prog("SELECT a, b, c FROM t LIMIT 7")
        self.assertEqual([c.name for c in p.select], ["a", "b", "c"])
        self.assertEqual(p.limit, cst.Limit(7, p.limit.offset))

    def test_or_has_lower_precedence_than_and(self):
        p = prog("SELECT a FROM t WHERE a = 1 OR b = 2 AND c = 3")
        w = p.where
        self.assertIsInstance(w, cst.Or)
        self.assertIsInstance(w.right, cst.And)

    def test_comparison_node_shapes(self):
        p = prog("SELECT a FROM t WHERE a >= 10")
        self.assertEqual(p.where, cst.Cmp(">=", cst.Ref("a", 22),
                                          cst.Lit(10, 27), 24))

    def test_parentheses_group_expressions(self):
        p = prog("SELECT a FROM t WHERE (a = 1 OR b = 2) AND c = 3")
        self.assertIsInstance(p.where, cst.And)
        self.assertIsInstance(p.where.left, cst.Or)

    def test_negative_limit_stored_as_signed_int(self):
        p = prog("SELECT a FROM t LIMIT -5")
        self.assertEqual(p.limit.value, -5)

    def test_program_is_immutable(self):
        p = prog("SELECT a FROM t")
        with self.assertRaises(AttributeError):
            p.table = None  # type: ignore[attr-defined]


class ParserBoundaryTests(unittest.TestCase):
    def test_eof_in_each_clause(self):
        cases = [
            ("", "SELECT"),
            ("SELECT", "a column name"),
            ("SELECT a", "FROM"),
            ("SELECT a FROM", "a table name"),
            ("SELECT a FROM t WHERE", "an expression"),
            ("SELECT a FROM t LIMIT", "a number"),
        ]
        for text, expected in cases:
            with self.subTest(text=text or "<empty>"):
                with self.assertRaises(ParseError) as ctx:
                    prog(text)
                self.assertEqual(ctx.exception.code,
                                 ErrorCode.UNEXPECTED_TOKEN)
                self.assertIn(expected, ctx.exception.message)

    def test_empty_column_list(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT FROM t")
        self.assertEqual(ctx.exception.offset, 7)
        self.assertIn("a column name", ctx.exception.message)

    def test_column_list_missing_after_comma(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a, FROM t")
        self.assertIn("a column name", ctx.exception.message)

    def test_column_list_comma_expected_before_bare_ident(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a b FROM t")
        self.assertIn("','", ctx.exception.message)
        self.assertEqual(ctx.exception.offset, 9)

    def test_unclosed_group_reports_eof_position(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a FROM t WHERE (a = 1")
        self.assertEqual(ctx.exception.code, ErrorCode.UNCLOSED_GROUP)
        self.assertEqual(ctx.exception.offset, len(
            "SELECT a FROM t WHERE (a = 1"))

    def test_limit_then_where_is_rejected_at_where(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a FROM t LIMIT 2 WHERE a = 1")
        self.assertEqual(ctx.exception.offset, 24)
        self.assertIn("end of query", ctx.exception.message)

    def test_duplicate_limit_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a FROM t LIMIT 1 LIMIT 2")
        self.assertIn("end of query", ctx.exception.message)
        self.assertEqual(ctx.exception.offset, 24)

    def test_duplicate_where_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a FROM t WHERE a = 1 WHERE b = 2")
        self.assertIn("LIMIT or end of query", ctx.exception.message)

    def test_trailing_junk_after_clause(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a FROM t EXTRA")
        self.assertEqual(ctx.exception.offset, 16)
        self.assertIn("LIMIT or end of query", ctx.exception.message)


class ParserMalformedTests(unittest.TestCase):
    def test_chained_comparison_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a FROM t WHERE a = 1 < 2")
        self.assertEqual(ctx.exception.code,
                         ErrorCode.UNEXPECTED_TOKEN)
        self.assertEqual(ctx.exception.offset, 28)
        self.assertIn("AND, OR or end of expression",
                      ctx.exception.message)

    def test_operator_where_expression_expected(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a FROM t WHERE = 1")
        self.assertIn("an expression", ctx.exception.message)

    def test_dangling_and(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a FROM t WHERE a = 1 AND")
        self.assertIn("an expression", ctx.exception.message)

    def test_limit_must_be_number(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a FROM t LIMIT abc")
        self.assertIn("a number", ctx.exception.message)

    def test_comma_after_table_is_structure_error(self):
        with self.assertRaises(ParseError) as ctx:
            prog("SELECT a FROM t, x")
        self.assertIn("LIMIT or end of query", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
