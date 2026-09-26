"""Stage 4: error representation and public error formatting.

``ParseError`` is the *only* way stages report failure.  It carries a
category, a human-readable message and an absolute character position.
``to_public`` converts it into the external error dict, deriving line/column
from the source text.  Stages never build public dicts themselves.
"""

LEX = "lex"
SYNTAX = "syntax"

CATEGORIES = (LEX, SYNTAX)


class ParseError(Exception):
    """Internal, stage-agnostic error.  Immutable by convention."""

    def __init__(self, category, message, pos):
        if category not in CATEGORIES:
            raise ValueError("unknown error category: %r" % category)
        super().__init__(message)
        self.category = category
        self.message = message
        self.pos = pos

    def __repr__(self):
        return "ParseError(%r, %r, %r)" % (self.category, self.message, self.pos)


def to_public(error, text):
    """Convert a ParseError into the external error dict.

    Pure function of (error, text); computes 1-based line/col from ``pos``.
    """
    pos = error.pos
    line = text.count("\n", 0, pos) + 1
    col = pos - text.rfind("\n", 0, pos)
    return {
        "category": error.category,
        "message": error.message,
        "pos": pos,
        "line": line,
        "col": col,
    }
