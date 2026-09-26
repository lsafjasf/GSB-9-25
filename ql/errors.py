"""Error model shared by every pipeline stage.

Stages never print messages or compute line/column themselves: they raise a
:class:`ParseError` carrying a stable ``code``, an offset into the source text
and parameters.  Conversion to the public ``{"error": ...}`` payload happens in
exactly one place (:func:`ParseError.to_dict`), which guarantees identical
classification, position and wording regardless of the originating stage.
"""

from __future__ import annotations

from dataclasses import dataclass


class ErrorCode:
    UNEXPECTED_CHAR = "E_UNEXPECTED_CHAR"
    UNCLOSED_STRING = "E_UNCLOSED_STRING"
    BAD_ESCAPE = "E_BAD_ESCAPE"
    UNEXPECTED_TOKEN = "E_UNEXPECTED_TOKEN"
    UNCLOSED_GROUP = "E_UNCLOSED_GROUP"
    DUPLICATE_COLUMN = "E_DUPLICATE_COLUMN"
    WHERE_CONSTANT = "E_WHERE_CONSTANT"
    LIMIT_RANGE = "E_LIMIT_RANGE"


@dataclass(frozen=True)
class ParseError(Exception):
    code: str
    offset: int
    message: str

    def __str__(self) -> str:
        return f"{self.code} at offset {self.offset}: {self.message}"


def unexpected_char(offset: int, ch: str) -> ParseError:
    return ParseError(
        ErrorCode.UNEXPECTED_CHAR, offset, f"unexpected character {ch!r}"
    )


def unclosed_string(offset: int) -> ParseError:
    return ParseError(
        ErrorCode.UNCLOSED_STRING, offset, "unterminated string literal"
    )


def bad_escape(offset: int, ch: str) -> ParseError:
    return ParseError(
        ErrorCode.BAD_ESCAPE, offset, f"invalid escape sequence \\{ch}"
    )


def unexpected_token(offset: int, got: str, expected: str) -> ParseError:
    return ParseError(
        ErrorCode.UNEXPECTED_TOKEN,
        offset,
        f"expected {expected} but found {got}",
    )


def unclosed_group(offset: int) -> ParseError:
    return ParseError(
        ErrorCode.UNCLOSED_GROUP, offset, "unclosed parenthesised group"
    )


def duplicate_column(offset: int, name: str) -> ParseError:
    return ParseError(
        ErrorCode.DUPLICATE_COLUMN, offset, f"duplicate column {name!r}"
    )


def where_constant(offset: int) -> ParseError:
    return ParseError(
        ErrorCode.WHERE_CONSTANT,
        offset,
        "WHERE condition must reference at least one column",
    )


def limit_range(offset: int, value: int) -> ParseError:
    return ParseError(
        ErrorCode.LIMIT_RANGE,
        offset,
        f"LIMIT must be between 0 and 1000, got {value}",
    )
