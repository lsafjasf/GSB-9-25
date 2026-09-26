"""Public entry point: wire the four stages together.

``parse(text)`` has exactly the same contract as the legacy monolith:

    {"ok": True,  "ast": <assembled dict>}
    {"ok": False, "error": {"category", "message", "pos", "line", "col"}}
"""

from .assemble import assemble
from .errors import ParseError, to_public
from .lexer import tokenize
from .structure import parse_structure


def parse(text):
    try:
        tokens = tokenize(text)          # stage 1: lexing
        node = parse_structure(tokens)   # stage 2: structure judgement
        ast = assemble(node)             # stage 3: result assembly
        return {"ok": True, "ast": ast}
    except ParseError as exc:            # stage 4: error generation
        return {"ok": False, "error": to_public(exc, text)}
