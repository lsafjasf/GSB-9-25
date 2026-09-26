"""Stage 2: structure judgement (recursive descent over tokens).

``parse_structure(tokens) -> Node`` builds the immutable structure tree and
raises ``ParseError(category=SYNTAX, ...)`` when the token stream does not
match the grammar.  Pure: consumes its input list read-only via an internal
cursor, keeps no state between calls.
"""

from .errors import SYNTAX, ParseError
from .nodes import And, Cmp, Not, Num, Or, Ref, Str
from .tokens import AND, IDENT, LPAREN, NOT, NUMBER, OP, OR, RPAREN, STRING


class _Cursor:
    """Private read cursor over an immutable token tuple."""

    __slots__ = ("tokens", "index")

    def __init__(self, tokens):
        self.tokens = tokens
        self.index = 0

    def peek(self):
        return self.tokens[self.index]

    def advance(self):
        tok = self.tokens[self.index]
        self.index += 1
        return tok


def parse_structure(tokens):
    cursor = _Cursor(tokens)
    node = _parse_or(cursor)
    if cursor.peek().kind != "EOF":
        raise ParseError(SYNTAX, "unexpected token", cursor.peek().pos)
    return node


def _parse_or(cursor):
    left = _parse_and(cursor)
    while cursor.peek().kind == OR:
        cursor.advance()
        left = Or(left, _parse_and(cursor))
    return left


def _parse_and(cursor):
    left = _parse_not(cursor)
    while cursor.peek().kind == AND:
        cursor.advance()
        left = And(left, _parse_not(cursor))
    return left


def _parse_not(cursor):
    if cursor.peek().kind == NOT:
        cursor.advance()
        return Not(_parse_not(cursor))
    return _parse_atom(cursor)


def _parse_atom(cursor):
    tok = cursor.peek()
    if tok.kind == LPAREN:
        cursor.advance()
        inner = _parse_or(cursor)
        if cursor.peek().kind != RPAREN:
            raise ParseError(SYNTAX, "expected ')'", cursor.peek().pos)
        cursor.advance()
        return inner
    if tok.kind == IDENT:
        cursor.advance()
        op = cursor.peek()
        if op.kind != OP:
            raise ParseError(SYNTAX, "expected comparison operator", op.pos)
        cursor.advance()
        val = cursor.peek()
        if val.kind == NUMBER:
            cursor.advance()
            value = Num(val.value)
        elif val.kind == STRING:
            cursor.advance()
            value = Str(val.value)
        elif val.kind == IDENT:
            cursor.advance()
            value = Ref(val.value)
        else:
            raise ParseError(SYNTAX, "expected value", val.pos)
        return Cmp(tok.value, op.value, value)
    raise ParseError(SYNTAX, "expected operand", tok.pos)
