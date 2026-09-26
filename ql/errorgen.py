"""Stage 4: error rendering.

Input  : a :class:`ql.errors.ParseError` plus the original source text.
Output : the public, JSON-serialisable error payload.

This is the *only* module that derives line/column from an offset, so every
stage reports positions identically and wording/classification live in one
catalogue (``ql.errors``).
"""

from __future__ import annotations

from .errors import ParseError


def render(error: ParseError, source: str) -> dict:
    line = source.count("\n", 0, error.offset) + 1
    last_newline = source.rfind("\n", 0, error.offset)
    column = error.offset + 1 if last_newline < 0 else error.offset - last_newline
    return {
        "ok": False,
        "error": {
            "code": error.code,
            "message": error.message,
            "offset": error.offset,
            "line": line,
            "column": column,
        },
    }
