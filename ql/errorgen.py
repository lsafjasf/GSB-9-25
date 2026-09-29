"""Stage 4: error rendering.

Input  : a :class:`ql.errors.ParseError` plus the original source text.
Output : the public, JSON-serialisable error payload.

This is the *only* module that derives line/column from an offset, so every
stage reports positions identically and wording/classification live in one
catalogue (``ql.errors``).

Recovery mode reuses the same position math to render a whole error list;
entries additionally expose the structured ``expected``/``actual`` pair.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

from .errors import ParseError


def _line_col(source: str, offset: int) -> Tuple[int, int]:
    line = source.count("\n", 0, offset) + 1
    last_newline = source.rfind("\n", 0, offset)
    column = offset + 1 if last_newline < 0 else offset - last_newline
    return line, column


def render(error: ParseError, source: str) -> dict:
    line, column = _line_col(source, error.offset)
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


def render_recovery(errors: Iterable[ParseError], source: str) -> List[dict]:
    """Render a recovery-mode error list, sorted by source position."""
    ordered = sorted(errors, key=lambda e: (e.offset, e.code, e.message))
    rendered = []
    for error in ordered:
        line, column = _line_col(source, error.offset)
        rendered.append({
            "code": error.code,
            "message": error.message,
            "offset": error.offset,
            "line": line,
            "column": column,
            "expected": error.expected,
            "actual": error.actual,
        })
    return rendered
