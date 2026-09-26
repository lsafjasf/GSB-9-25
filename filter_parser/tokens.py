"""Token type produced by stage 1 and consumed by stage 2.

A NamedTuple keeps tokens immutable and typed while staying cheap to
construct (lexing is the hottest stage).
"""

from typing import NamedTuple

# Token kinds
IDENT = "IDENT"
NUMBER = "NUMBER"
STRING = "STRING"
OP = "OP"
AND = "AND"
OR = "OR"
NOT = "NOT"
LPAREN = "LPAREN"
RPAREN = "RPAREN"
EOF = "EOF"


class Token(NamedTuple):
    kind: str
    value: object  # str for IDENT/OP/STRING, int|float for NUMBER, None else
    pos: int       # absolute offset of the first character in the source
