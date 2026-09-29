"""Public facade: wires the four stages together.

    source --[lexer]--> tokens --[parser]--> CST --[assembler]--> result
                                 \\                               /
                                  `---[errorgen]--> error payload

Two modes:

* ``mode="strict"`` (default): first problem aborts the parse and yields
  the classic single-error payload — byte-identical to earlier releases;
* ``mode="recover"``: unrecognisable fragments are skipped, scanning
  continues, and the caller gets a partial result plus a full error list
  (position, expected, actual for every problem found).
"""

from __future__ import annotations

from .assembler import assemble, assemble_recover
from .errorgen import render, render_recovery
from .errors import ParseError
from .lexer import tokenize, tokenize_recover
from .parser import parse, parse_recover


def parse_query(source: str, mode: str = "strict") -> dict:
    """Parse ``source`` and always return a plain JSON-serialisable dict.

    ``mode`` is ``"strict"`` (fail on the first error) or ``"recover"``
    (skip bad fragments, return partial results plus an error list).
    """
    if mode == "recover":
        return _parse_recover(source)
    if mode != "strict":
        raise ValueError(f"unknown parse mode: {mode!r}")
    try:
        tokens = tokenize(source)
        program = parse(tokens, len(source))
        return assemble(program)
    except ParseError as error:
        return render(error, source)


def _parse_recover(source: str) -> dict:
    tokens, lex_errors = tokenize_recover(source)
    program, parse_errors = parse_recover(tokens, len(source))
    query, semantic_errors = assemble_recover(program)
    errors = render_recovery(
        (*lex_errors, *parse_errors, *semantic_errors), source
    )
    return {"ok": not errors, "query": query, "errors": errors}
