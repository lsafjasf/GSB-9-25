"""Stage 1: tokenisation.

Input  : the raw ``str`` query text.
Output : a ``tuple[Token, ...]`` — immutable, no position state leaks out.

The scanner owns a local index only; it never mutates module globals.  All
lexical problems are raised as :class:`ql.errors.ParseError` with an offset so
later position rendering stays in one place.

Two modes share one scanner:

* ``tokenize`` (strict) raises on the first problem;
* ``tokenize_recover`` collects problems into an error list, skips the
  offending fragment and keeps scanning, so later stages still get every
  recognisable token.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Tuple

from .errors import (
    ParseError,
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
    tokens, _ = _scan(source, None)
    return tokens


def tokenize_recover(
    source: str,
) -> tuple[tuple[Token, ...], tuple[ParseError, ...]]:
    """Tokenise, skipping unrecognisable fragments and collecting errors."""
    tokens, errors = _scan(source, [])
    return tokens, tuple(errors)


def _scan(
    source: str, errors: Optional[List[ParseError]]
) -> Tuple[tuple[Token, ...], Tuple[ParseError, ...]]:
    """Shared scanner.  ``errors is None`` selects strict (raising) mode."""
    tokens: list[Token] = []
    collected: List[ParseError] = errors if errors is not None else []
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
            tok, i = _read_string(source, start, errors)
            if tok is not None:
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

        error = unexpected_char(start, ch)
        if errors is None:
            raise error
        collected.append(error)
        i += 1
        continue

    return tuple(tokens), tuple(collected)


def _read_string(
    source: str, start: int, errors: Optional[List[ParseError]]
) -> tuple[Optional[Token], int]:
    """Scan a string literal starting at ``start`` (the opening quote).

    Returns the token plus the index immediately after the closing quote.
    In recovery mode a broken literal yields ``None`` (the fragment is
    skipped) and scanning resumes at a safe point.
    """
    out: list[str] = []
    i = start + 1
    n = len(source)
    while i < n and source[i] != '"':
        ch = source[i]
        if ch == "\n":
            error = unclosed_string(start)
            if errors is None:
                raise error
            errors.append(error)
            return None, i  # resume at the newline
        if ch == "\\":
            if i + 1 >= n or source[i + 1] == "\n":
                error = bad_escape(i + 1 if i + 1 < n else i, "")
                if errors is None:
                    raise error
                errors.append(error)
                return None, i + 1  # nothing useful left in this literal
            esc = source[i + 1]
            mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
            if esc not in mapping:
                error = bad_escape(i + 1, esc)
                if errors is None:
                    raise error
                errors.append(error)
                out.append(esc)  # keep the char, drop the backslash
                i += 2
                continue
            out.append(mapping[esc])
            i += 2
        else:
            out.append(ch)
            i += 1

    if i >= n:
        error = unclosed_string(start)
        if errors is None:
            raise error
        errors.append(error)
        return None, i

    return Token("STRING", "".join(out), start, source[start : i + 1]), i + 1
