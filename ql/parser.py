"""Stage 2: structure determination (tokens -> CST).

Input  : ``tuple[Token, ...]`` plus the source length (EOF offset).
Output : a :class:`ql.cst.Program` immutable syntax tree.

The parser is a hand-written recursive-descent parser with one cursor local to
the :class:`Parser` instance.  It never looks at source text and never builds
the public result shape — that is stage 3's job.
"""

from __future__ import annotations

from typing import Optional

from . import cst
from .cst import And, Cmp, Lit, Limit, Or, Program, Ref
from .errors import ParseError, unexpected_token, unclosed_group
from .lexer import Token

_COMPARISONS = frozenset({"=", "<>", "!=", "<", "<=", ">", ">="})
_EXPR_END_DISPLAY = "AND, OR or end of expression"


def describe(tok: Optional[Token]) -> str:
    """User-facing spelling of a token (or EOF) in error messages."""
    if tok is None:
        return "end of query"
    if tok.kind == "KEYWORD":
        return tok.value  # type: ignore[return-value]
    if tok.kind == "STRING":
        return tok.raw
    if tok.kind == "NUMBER":
        return str(tok.value)
    return tok.value  # type: ignore[return-value]


class Parser:
    def __init__(self, tokens: tuple[Token, ...], eof_offset: int):
        self._toks = tokens
        self._pos = 0
        self._eof = eof_offset

    # ---- cursor helpers -------------------------------------------------
    def _peek(self) -> Optional[Token]:
        return self._toks[self._pos] if self._pos < len(self._toks) else None

    def _advance(self) -> Token:
        tok = self._toks[self._pos]
        self._pos += 1
        return tok

    def _offset(self) -> int:
        tok = self._peek()
        return self._eof if tok is None else tok.offset

    def _take_keyword(self, word: str) -> bool:
        tok = self._peek()
        if tok is not None and tok.kind == "KEYWORD" and tok.value == word:
            self._advance()
            return True
        return False

    def _fail(self, expected: str) -> ParseError:
        tok = self._peek()
        return unexpected_token(self._offset(), describe(tok), expected)

    # ---- entry point ----------------------------------------------------
    def parse_program(self) -> Program:
        if not self._take_keyword("SELECT"):
            raise self._fail("SELECT")

        columns = [self._expect_column()]
        while True:
            tok = self._peek()
            if tok is not None and tok.kind == "OP" and tok.value == ",":
                self._advance()
                columns.append(self._expect_column())
                continue
            if tok is not None and tok.kind == "IDENT":
                raise self._fail("','")
            break

        if not self._take_keyword("FROM"):
            raise self._fail("FROM")
        table = self._expect_identifier("a table name")

        where = None
        limit = None
        seen_where = False
        seen_limit = False
        while True:
            nxt = self._peek()
            if nxt is None:
                break
            if nxt.kind == "KEYWORD" and nxt.value == "WHERE":
                if seen_limit:
                    raise self._fail("end of query")
                if seen_where:
                    raise self._fail("LIMIT or end of query")
                self._advance()
                seen_where = True
                where = self._parse_or()
            elif nxt.kind == "KEYWORD" and nxt.value == "LIMIT":
                if seen_limit:
                    raise self._fail("end of query")
                self._advance()
                seen_limit = True
                limit = self._parse_limit()
            else:
                raise self._fail("LIMIT or end of query")

        return Program(tuple(columns), table, where, limit)

    def _expect_column(self) -> Ref:
        tok = self._peek()
        if tok is None or tok.kind != "IDENT":
            raise self._fail("a column name")
        self._advance()
        return Ref(tok.value, tok.offset)  # type: ignore[arg-type]

    def _expect_identifier(self, expected: str) -> Ref:
        tok = self._peek()
        if tok is None or tok.kind != "IDENT":
            raise self._fail(expected)
        self._advance()
        return Ref(tok.value, tok.offset)  # type: ignore[arg-type]

    def _parse_limit(self) -> Limit:
        negate = False
        tok = self._peek()
        if tok is not None and tok.kind == "OP" and tok.value == "-":
            self._advance()
            negate = True
        tok = self._peek()
        if tok is None or tok.kind != "NUMBER":
            raise self._fail("a number")
        self._advance()
        value = -tok.value if negate else tok.value
        return Limit(value, tok.offset)  # type: ignore[operator]

    # ---- expression grammar (lowest to highest precedence) --------------
    def _parse_or(self):
        left = self._parse_and()
        while True:
            tok = self._peek()
            if not (tok is not None and tok.kind == "KEYWORD"
                    and tok.value == "OR"):
                return left
            self._advance()
            op_offset = tok.offset
            right = self._parse_and()
            left = Or(left, right, op_offset)

    def _parse_and(self):
        left = self._parse_compare()
        while True:
            tok = self._peek()
            if not (tok is not None and tok.kind == "KEYWORD"
                    and tok.value == "AND"):
                return left
            self._advance()
            op_offset = tok.offset
            right = self._parse_compare()
            left = And(left, right, op_offset)

    def _parse_compare(self):
        left = self._parse_atom()
        tok = self._peek()
        if tok is None or not (tok.kind == "OP" and tok.value in _COMPARISONS):
            return left
        self._advance()
        op_offset = tok.offset
        right = self._parse_atom()
        chained = self._peek()
        if (
            chained is not None
            and chained.kind == "OP"
            and chained.value in _COMPARISONS
        ):
            raise unexpected_token(
                chained.offset, describe(chained), _EXPR_END_DISPLAY
            )
        return Cmp(tok.value, left, right, op_offset)

    def _parse_atom(self):
        tok = self._peek()
        if tok is None:
            raise self._fail("an expression")
        if tok.kind in ("NUMBER", "STRING"):
            self._advance()
            return Lit(tok.value, tok.offset)
        if tok.kind == "IDENT":
            self._advance()
            return Ref(tok.value, tok.offset)  # type: ignore[arg-type]
        if tok.kind == "OP" and tok.value == "(":
            self._advance()
            expr = self._parse_or()
            close = self._peek()
            if close is None or not (
                close.kind == "OP" and close.value == ")"
            ):
                raise unclosed_group(self._offset())
            self._advance()
            return expr
        raise self._fail("an expression")


def parse(tokens: tuple[Token, ...], eof_offset: int) -> Program:
    return Parser(tokens, eof_offset).parse_program()
