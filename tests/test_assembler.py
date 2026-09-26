"""Stage 3 unit tests: assembly + semantic validation (CST -> result)."""

import unittest

from ql import cst
from ql.assembler import assemble
from ql.errors import ErrorCode, ParseError
from ql.lexer import tokenize
from ql.parser import parse


def assemble_text(text):
    return assemble(parse(tokenize(text), len(text)))


class AssemblerNormalTests(unittest.TestCase):
    def test_minimal_shape(self):
        self.assertEqual(
            assemble_text("SELECT a FROM t"),
            {"ok": True, "query": {
                "select": ["a"], "from": "t",
                "where": None, "limit": None}},
        )

    def test_full_shape_and_comparison(self):
        out = assemble_text(
            'SELECT a FROM t WHERE a <> "x" LIMIT 10')
        query = out["query"]
        self.assertEqual(query["select"], ["a"])
        self.assertEqual(query["from"], "t")
        self.assertEqual(query["limit"], 10)
        self.assertEqual(query["where"], {
            "type": "cmp", "op": "<>",
            "left": {"type": "ref", "name": "a"},
            "right": {"type": "literal", "value": "x"},
        })

    def test_and_or_chains_are_flattened(self):
        where = assemble_text(
            "SELECT a FROM t WHERE a = 1 AND b = 2 AND c = 3"
        )["query"]["where"]
        self.assertEqual(where["type"], "and")
        self.assertEqual(len(where["args"]), 3)

        where = assemble_text(
            "SELECT a FROM t WHERE (a = 1 OR b = 2) OR c = 3"
        )["query"]["where"]
        self.assertEqual(where["type"], "or")
        self.assertEqual(len(where["args"]), 3)

    def test_nested_operators_keep_shape(self):
        where = assemble_text(
            "SELECT a FROM t WHERE a = 1 OR b = 2 AND c = 3"
        )["query"]["where"]
        self.assertEqual(where["type"], "or")
        self.assertEqual(where["args"][1]["type"], "and")


class AssemblerBoundaryTests(unittest.TestCase):
    def test_limit_bounds_inclusive(self):
        self.assertEqual(assemble_text(
            "SELECT a FROM t LIMIT 0")["query"]["limit"], 0)
        self.assertEqual(assemble_text(
            "SELECT a FROM t LIMIT 1000")["query"]["limit"], 1000)

    def test_zero_negative_limit_is_zero(self):
        self.assertEqual(assemble_text(
            "SELECT a FROM t LIMIT -0")["query"]["limit"], 0)

    def test_column_names_can_match_keyword_letters_exactly_not_case(self):
        out = assemble_text("SELECT selectx FROM tabley")
        self.assertEqual(out["query"]["select"], ["selectx"])


class AssemblerMalformedTests(unittest.TestCase):
    def test_duplicate_column_points_at_second_occurrence(self):
        with self.assertRaises(ParseError) as ctx:
            assemble_text("SELECT a, b, a FROM t")
        self.assertEqual(ctx.exception.code,
                         ErrorCode.DUPLICATE_COLUMN)
        self.assertEqual(ctx.exception.offset, 13)
        self.assertIn("'a'", ctx.exception.message)

    def test_constant_where_literal(self):
        with self.assertRaises(ParseError) as ctx:
            assemble_text("SELECT a FROM t WHERE 1")
        self.assertEqual(ctx.exception.code,
                         ErrorCode.WHERE_CONSTANT)

    def test_constant_where_comparison_of_two_literals(self):
        with self.assertRaises(ParseError) as ctx:
            assemble_text('SELECT a FROM t WHERE 1 = "x"')
        self.assertEqual(ctx.exception.code,
                         ErrorCode.WHERE_CONSTANT)

    def test_constant_where_inside_group_and_or(self):
        with self.assertRaises(ParseError) as ctx:
            assemble_text("SELECT a FROM t WHERE (1 = 2 OR 3)")
        self.assertEqual(ctx.exception.code,
                         ErrorCode.WHERE_CONSTANT)

    def test_limit_above_range(self):
        with self.assertRaises(ParseError) as ctx:
            assemble_text("SELECT a FROM t LIMIT 1001")
        self.assertEqual(ctx.exception.code, ErrorCode.LIMIT_RANGE)
        self.assertIn("1001", ctx.exception.message)

    def test_limit_below_range_reports_signed_value(self):
        with self.assertRaises(ParseError) as ctx:
            assemble_text("SELECT a FROM t LIMIT -1")
        self.assertEqual(ctx.exception.code, ErrorCode.LIMIT_RANGE)
        self.assertIn("-1", ctx.exception.message)

    def test_assembler_works_on_hand_built_cst_without_tokens(self):
        program = cst.Program(
            select=(cst.Ref("a", 0),),
            table=cst.Ref("t", 0),
            where=cst.Lit(1, 0),
            limit=None,
        )
        with self.assertRaises(ParseError) as ctx:
            assemble(program)
        self.assertEqual(ctx.exception.code,
                         ErrorCode.WHERE_CONSTANT)


if __name__ == "__main__":
    unittest.main()
