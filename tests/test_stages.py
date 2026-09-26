"""Per-stage unit tests: lexer, structure, assemble, errors, api."""

import unittest

from filter_parser import (
    LEX, SYNTAX, ParseError, assemble, parse, parse_structure, to_public,
    tokenize,
)
from filter_parser.nodes import And, Cmp, Not, Num, Or, Ref, Str
from filter_parser.tokens import (
    AND, EOF, IDENT, LPAREN, NOT, NUMBER, OP, OR, RPAREN, STRING, Token,
)


def kinds(tokens):
    return [t.kind for t in tokens]


class LexerTests(unittest.TestCase):
    # --- normal ---
    def test_simple_comparison_tokens(self):
        toks = tokenize("age >= 18")
        self.assertEqual(kinds(toks), [IDENT, OP, NUMBER, EOF])
        self.assertEqual(toks[0], Token(IDENT, "age", 0))
        self.assertEqual(toks[1], Token(OP, ">=", 4))
        self.assertEqual(toks[2], Token(NUMBER, 18, 7))
        self.assertEqual(toks[3], Token(EOF, None, 9))

    def test_keywords_and_idents(self):
        toks = tokenize("and or not anderson")
        self.assertEqual(kinds(toks), [AND, OR, NOT, IDENT, EOF])
        self.assertEqual(toks[3].value, "anderson")

    def test_all_operators(self):
        toks = tokenize("= != > >= < <=")
        self.assertEqual([t.value for t in toks[:-1]],
                         ["=", "!=", ">", ">=", "<", "<="])

    def test_string_escapes(self):
        toks = tokenize(r"'a\n\t\\\'b'")
        self.assertEqual(toks[0].value, "a\n\t\\'b")

    def test_double_quoted_string(self):
        toks = tokenize('"hi \'x\'"')
        self.assertEqual(toks[0].value, "hi 'x'")

    def test_positions_with_whitespace(self):
        toks = tokenize("  a   =   1")
        self.assertEqual([(t.kind, t.pos) for t in toks],
                         [(IDENT, 2), (OP, 6), (NUMBER, 10), (EOF, 11)])

    # --- boundary ---
    def test_empty_input(self):
        self.assertEqual(tokenize(""), [Token(EOF, None, 0)])

    def test_whitespace_only(self):
        self.assertEqual(tokenize(" \t\r\n"), [Token(EOF, None, 4)])

    def test_int_vs_float(self):
        toks = tokenize("1 1.0 0.5")
        self.assertIs(type(toks[0].value), int)
        self.assertIs(type(toks[1].value), float)
        self.assertEqual(toks[2].value, 0.5)

    def test_big_int(self):
        toks = tokenize("123456789012345678901234567890")
        self.assertEqual(toks[0].value, 123456789012345678901234567890)

    def test_parens(self):
        self.assertEqual(kinds(tokenize("()")), [LPAREN, RPAREN, EOF])

    # --- malformed ---
    def test_unexpected_character(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize("a = @")
        self.assertEqual(ctx.exception.category, LEX)
        self.assertEqual(ctx.exception.message, "unexpected character '@'")
        self.assertEqual(ctx.exception.pos, 4)

    def test_unterminated_string(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize("x = 'abc")
        self.assertEqual(ctx.exception.category, LEX)
        self.assertEqual(ctx.exception.message, "unterminated string literal")
        self.assertEqual(ctx.exception.pos, 4)  # position of opening quote

    def test_unterminated_string_backslash_at_eof(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize("'ab\\")
        self.assertEqual(ctx.exception.message, "unterminated string literal")
        self.assertEqual(ctx.exception.pos, 0)

    def test_invalid_escape(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize("'a\\q'")
        self.assertEqual(ctx.exception.message, "invalid escape sequence '\\q'")
        self.assertEqual(ctx.exception.pos, 2)  # position of the backslash

    def test_malformed_number_trailing_dot(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize("12.")
        self.assertEqual(ctx.exception.message, "malformed number literal")
        self.assertEqual(ctx.exception.pos, 2)  # position of the dot

    def test_malformed_number_ident_suffix(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize("1abc")
        self.assertEqual(ctx.exception.message, "malformed number literal")
        self.assertEqual(ctx.exception.pos, 0)

    def test_no_shared_state_between_calls(self):
        tokenize("a = 1")
        tokenize("b = 2")
        self.assertEqual(kinds(tokenize("c = 3")), [IDENT, OP, NUMBER, EOF])


class StructureTests(unittest.TestCase):
    def struct(self, text):
        return parse_structure(tokenize(text))

    # --- normal ---
    def test_single_cmp(self):
        self.assertEqual(self.struct("age >= 18"),
                         Cmp("age", ">=", Num(18)))

    def test_and_or_precedence(self):
        # and binds tighter: a or (b and c)
        self.assertEqual(
            self.struct("a = 1 or b = 2 and c = 3"),
            Or(Cmp("a", "=", Num(1)),
               And(Cmp("b", "=", Num(2)), Cmp("c", "=", Num(3)))))

    def test_left_associativity(self):
        self.assertEqual(
            self.struct("a = 1 and b = 2 and c = 3"),
            And(And(Cmp("a", "=", Num(1)), Cmp("b", "=", Num(2))),
                Cmp("c", "=", Num(3))))

    def test_not_binds_tightest(self):
        self.assertEqual(self.struct("not a = 1 and b = 2"),
                         And(Not(Cmp("a", "=", Num(1))), Cmp("b", "=", Num(2))))

    def test_parens(self):
        self.assertEqual(self.struct("(a = 1 or b = 2) and c = 3"),
                         And(Or(Cmp("a", "=", Num(1)), Cmp("b", "=", Num(2))),
                             Cmp("c", "=", Num(3))))

    def test_value_variants(self):
        self.assertEqual(self.struct("a = 'x'"), Cmp("a", "=", Str("x")))
        self.assertEqual(self.struct("a = b"), Cmp("a", "=", Ref("b")))
        self.assertEqual(self.struct("a = 1.5"), Cmp("a", "=", Num(1.5)))

    def test_hand_built_token_list_is_accepted(self):
        # Stage input is just a token sequence; no lexer involvement needed.
        tokens = [
            Token(IDENT, "x", 0), Token(OP, "=", 2), Token(NUMBER, 5, 4),
            Token(EOF, None, 5),
        ]
        self.assertEqual(parse_structure(tokens), Cmp("x", "=", Num(5)))

    # --- malformed ---
    def assert_syntax(self, text, message, pos):
        with self.assertRaises(ParseError) as ctx:
            self.struct(text)
        self.assertEqual(ctx.exception.category, SYNTAX)
        self.assertEqual(ctx.exception.message, message)
        self.assertEqual(ctx.exception.pos, pos)

    def test_expected_operand_empty(self):
        self.assert_syntax("", "expected operand", 0)

    def test_expected_operand_keyword(self):
        self.assert_syntax("and a = 1", "expected operand", 0)

    def test_expected_operator(self):
        self.assert_syntax("age 18", "expected comparison operator", 4)

    def test_expected_operator_at_eof(self):
        self.assert_syntax("age", "expected comparison operator", 3)

    def test_expected_value(self):
        self.assert_syntax("age = and", "expected value", 6)

    def test_expected_value_at_eof(self):
        self.assert_syntax("age =", "expected value", 5)

    def test_expected_rparen(self):
        self.assert_syntax("(a = 1", "expected ')'", 6)

    def test_unexpected_trailing_token(self):
        self.assert_syntax("a = 1 2", "unexpected token", 6)

    def test_unexpected_rparen(self):
        self.assert_syntax("a = 1)", "unexpected token", 5)


class AssembleTests(unittest.TestCase):
    def test_cmp_number(self):
        self.assertEqual(assemble(Cmp("age", ">=", Num(18))),
                         {"type": "cmp", "field": "age", "op": ">=",
                          "value": 18})

    def test_cmp_string_and_ref(self):
        self.assertEqual(assemble(Cmp("n", "=", Str("x")))["value"], "x")
        self.assertEqual(assemble(Cmp("n", "=", Ref("y")))["value"],
                         {"type": "ref", "name": "y"})

    def test_logical_nodes(self):
        a, b = Cmp("a", "=", Num(1)), Cmp("b", "=", Num(2))
        self.assertEqual(assemble(Or(a, b))["type"], "or")
        self.assertEqual(assemble(And(a, b))["type"], "and")
        self.assertEqual(assemble(Not(a)),
                         {"type": "not",
                          "operand": {"type": "cmp", "field": "a", "op": "=",
                                      "value": 1}})

    def test_nested_tree(self):
        tree = And(Or(Cmp("a", "=", Num(1)), Cmp("b", "=", Str("s"))),
                   Not(Cmp("c", "<", Ref("d"))))
        self.assertEqual(
            assemble(tree),
            {"type": "and",
             "left": {"type": "or",
                      "left": {"type": "cmp", "field": "a", "op": "=",
                               "value": 1},
                      "right": {"type": "cmp", "field": "b", "op": "=",
                                "value": "s"}},
             "right": {"type": "not",
                       "operand": {"type": "cmp", "field": "c", "op": "<",
                                   "value": {"type": "ref", "name": "d"}}}})

    def test_int_float_distinction_preserved(self):
        self.assertIs(type(assemble(Cmp("n", "=", Num(1)))["value"]), int)
        self.assertIs(type(assemble(Cmp("n", "=", Num(1.0)))["value"]), float)

    def test_unknown_node_rejected(self):
        with self.assertRaises(TypeError):
            assemble(object())


class ErrorStageTests(unittest.TestCase):
    def test_line_col_single_line(self):
        err = ParseError(LEX, "boom", 4)
        self.assertEqual(to_public(err, "a = @"),
                         {"category": "lex", "message": "boom",
                          "pos": 4, "line": 1, "col": 5})

    def test_line_col_multiline(self):
        text = "a = 1 and\n  b 2"
        err = ParseError(SYNTAX, "expected comparison operator", 14)
        pub = to_public(err, text)
        self.assertEqual((pub["line"], pub["col"]), (2, 5))

    def test_pos_at_newline_start(self):
        err = ParseError(LEX, "x", 6)
        pub = to_public(err, "a = 1\n@")
        self.assertEqual((pub["line"], pub["col"]), (2, 1))

    def test_unknown_category_rejected(self):
        with self.assertRaises(ValueError):
            ParseError("nope", "msg", 0)


class ApiTests(unittest.TestCase):
    def test_ok_shape(self):
        res = parse("a = 1")
        self.assertEqual(set(res), {"ok", "ast"})
        self.assertTrue(res["ok"])

    def test_error_shape(self):
        res = parse("a = ")
        self.assertEqual(set(res), {"ok", "error"})
        self.assertFalse(res["ok"])
        self.assertEqual(set(res["error"]),
                         {"category", "message", "pos", "line", "col"})

    def test_end_to_end(self):
        res = parse("not (a = 1 or b = 'x') and c != d")
        self.assertTrue(res["ok"])
        self.assertEqual(res["ast"]["type"], "and")
        self.assertEqual(res["ast"]["left"]["type"], "not")


if __name__ == "__main__":
    unittest.main()
