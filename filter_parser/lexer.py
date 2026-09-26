"""Stage 1: tokenize source text.

``tokenize(text) -> list[Token]`` (always ends with an EOF token).
Raises ``ParseError(category=LEX, ...)`` on malformed input.
Pure: no shared state, output depends only on ``text``.
"""

from .errors import LEX, ParseError
from .tokens import (
    AND, EOF, IDENT, LPAREN, NOT, NUMBER, OP, OR, RPAREN, STRING, Token,
)

_DIGITS = "0123456789"
_WS = " \t\r\n"
_KEYWORDS = {"and": AND, "or": OR, "not": NOT}
_TWO_CHAR_OPS = ("!=", ">=", "<=")
_ONE_CHAR_OPS = "=><"
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}


def _is_ident_start(ch):
    return ch == "_" or ("a" <= ch <= "z") or ("A" <= ch <= "Z")


def _is_ident_part(ch):
    return _is_ident_start(ch) or ("0" <= ch <= "9")


def tokenize(text):
    n = len(text)
    tokens = []
    i = 0
    while i < n:
        ch = text[i]
        if ch in _WS:
            i += 1
            continue

        if _is_ident_start(ch):
            start = i
            while i < n and _is_ident_part(text[i]):
                i += 1
            word = text[start:i]
            kind = _KEYWORDS.get(word)
            if kind is not None:
                tokens.append(Token(kind, None, start))
            else:
                tokens.append(Token(IDENT, word, start))
            continue

        if ch in _DIGITS:
            start = i
            while i < n and text[i] in _DIGITS:
                i += 1
            is_float = False
            if i < n and text[i] == ".":
                if i + 1 < n and text[i + 1] in _DIGITS:
                    is_float = True
                    i += 1
                    while i < n and text[i] in _DIGITS:
                        i += 1
                else:
                    raise ParseError(LEX, "malformed number literal", i)
            if i < n and _is_ident_part(text[i]):
                raise ParseError(LEX, "malformed number literal", start)
            raw = text[start:i]
            tokens.append(Token(NUMBER, float(raw) if is_float else int(raw), start))
            continue

        if ch == "'" or ch == '"':
            start = i
            quote = ch
            i += 1
            buf = []
            closed = False
            while i < n:
                c = text[i]
                if c == "\\":
                    if i + 1 >= n:
                        raise ParseError(LEX, "unterminated string literal", start)
                    esc = text[i + 1]
                    if esc not in _ESCAPES:
                        raise ParseError(
                            LEX, "invalid escape sequence '\\" + esc + "'", i
                        )
                    buf.append(_ESCAPES[esc])
                    i += 2
                    continue
                if c == quote:
                    closed = True
                    i += 1
                    break
                buf.append(c)
                i += 1
            if not closed:
                raise ParseError(LEX, "unterminated string literal", start)
            tokens.append(Token(STRING, "".join(buf), start))
            continue

        two = text[i:i + 2]
        if two in _TWO_CHAR_OPS:
            tokens.append(Token(OP, two, i))
            i += 2
            continue
        if ch in _ONE_CHAR_OPS:
            tokens.append(Token(OP, ch, i))
            i += 1
            continue
        if ch == "(":
            tokens.append(Token(LPAREN, None, i))
            i += 1
            continue
        if ch == ")":
            tokens.append(Token(RPAREN, None, i))
            i += 1
            continue

        raise ParseError(LEX, "unexpected character %r" % ch, i)

    tokens.append(Token(EOF, None, n))
    return tokens
