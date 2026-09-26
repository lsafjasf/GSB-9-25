"""Public facade: wires the four stages together.

    source --[lexer]--> tokens --[parser]--> CST --[assembler]--> result
                                 \\                               /
                                  `---[errorgen]--> error payload
"""

from __future__ import annotations

from .assembler import assemble
from .errorgen import render
from .errors import ParseError
from .lexer import tokenize
from .parser import parse


def parse_query(source: str) -> dict:
    """Parse ``source`` and always return a plain JSON-serialisable dict."""
    try:
        tokens = tokenize(source)
        program = parse(tokens, len(source))
        return assemble(program)
    except ParseError as error:
        return render(error, source)
