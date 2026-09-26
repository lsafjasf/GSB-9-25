"""Stage 1: tokenisation.

Input  : the raw ``str`` query text.
Output : a ``tuple[Token, ...]`` — immutable, no position state leaks out.

The scanner owns a local index only; it never mutates module globals.  All
lexical problems are raised as :class:`ql.errors.ParseError` with an offset so
later position rendering stays in one place.
"""

from __future__ import annotations

from typing import NamedTuple

from .errors import (
    bad_escape,
    unclosed_string,
    unexpected_char,
)

KEYWORDS = frozenset({"SELECT", "FROM", "WHERE", "LIMIT", "AND", "OR"})

_DIGITS = frozenset("0123456789")
_SPACES = frozenset(" \t\r\n")
# Identifiers stay ASCII on purpose so the contract is easy to pin down.
_ID_START = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
_ID_CONT = _ID_START | set("0123456789")
_SIMPLE_OPS = frozenset("(),=-")
_TWO_CHAR_OPS = frozenset(("<=", ">=", "<>", "!="))
_GT_LT = frozenset("<>")


class Token(NamedTuple):
    kind: str       # KEYWORD | IDENT | STRING | NUMBER | OP
    value: object   # str for KEYWORD/IDENT/STRING/OP, int for NUMBER
    offset: int     # offset of the first character in the source
    raw: str        # verbatim source spelling


def tokenize(source: str) -> tuple[Token, ...]:
    tokens: list[Token] = []
    n = len(source)
    i = 0

    while i < n:
        ch = source[i]

        if ch in _SPACES:
            i += 1
            continue

        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            i += 2
            while i < n and source[i] != "\n":
                i += 1
            continue

        start = i

        if ch == '"':
            tok, i = _read_string(source, start)
            tokens.append(tok)
            continue

        if ch in _DIGITS:
            j = i + 1
            while j < n and source[j] in _DIGITS:
                j += 1
            tokens.append(
                Token("NUMBER", int(source[i:j]), start, source[i:j])
            )
            i = j
            continue

        if ch in _ID_START:
            j = i + 1
            while j < n and source[j] in _ID_CONT:
                j += 1
            word = source[i:j]
            upper = word.upper()
            kind = "KEYWORD" if upper in KEYWORDS else "IDENT"
            tokens.append(Token(kind, upper if kind == "KEYWORD" else word,
                                start, word))
            i = j
            continue

        if i + 1 < n and source[i : i + 2] in _TWO_CHAR_OPS:
            two = source[i : i + 2]
            tokens.append(Token("OP", two, start, two))
            i += 2
            continue

        if ch in _GT_LT:
            tokens.append(Token("OP", ch, start, ch))
            i += 1
            continue

        if ch in _SIMPLE_OPS:
            tokens.append(Token("OP", ch, start, ch))
            i += 1
            continue

        raise unexpected_char(start, ch)

    return tuple(tokens)


def _read_string(source: str, start: int) -> tuple[Token, int]:
    """Scan a string literal starting at ``start`` (the opening quote).

    Returns the token plus the index immediately after the closing quote.
    """
    out: list[str] = []
    i = start + 1
    n = len(source)
    while i < n and source[i] != '"':
        ch = source[i]
        if ch == "\n":
            raise unclosed_string(start)
        if ch == "\\":
            if i + 1 >= n or source[i + 1] == "\n":
                raise bad_escape(i + 1 if i + 1 < n else i, "")
            esc = source[i + 1]
            mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
            if esc not in mapping:
                raise bad_escape(i + 1, esc)
            out.append(mapping[esc])
            i += 2
        else:
            out.append(ch)
            i += 1

    if i >= n:
        raise unclosed_string(start)

    return Token("STRING", "".join(out), start, source[start : i + 1]), i + 1
