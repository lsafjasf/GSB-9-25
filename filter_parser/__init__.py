"""Staged filter-expression parser (post-refactor).

Pipeline: tokenize -> parse_structure -> assemble, with ParseError ->
public error dict as the single error path.  No stage shares mutable state
with another; each is a pure function of its inputs.
"""

from .api import parse
from .assemble import assemble
from .errors import LEX, SYNTAX, ParseError, to_public
from .lexer import tokenize
from .structure import parse_structure

__all__ = [
    "parse", "tokenize", "parse_structure", "assemble",
    "ParseError", "to_public", "LEX", "SYNTAX",
]
