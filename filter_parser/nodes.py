"""Structure-tree node types produced by stage 2, consumed by stage 3.

All nodes are frozen dataclasses: the tree is immutable and can be shared
freely between stages without defensive copies.
"""

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True, slots=True)
class Or:
    left: "Node"
    right: "Node"


@dataclass(frozen=True, slots=True)
class And:
    left: "Node"
    right: "Node"


@dataclass(frozen=True, slots=True)
class Not:
    operand: "Node"


@dataclass(frozen=True, slots=True)
class Num:
    value: Union[int, float]


@dataclass(frozen=True, slots=True)
class Str:
    value: str


@dataclass(frozen=True, slots=True)
class Ref:
    name: str


Value = Union[Num, Str, Ref]


@dataclass(frozen=True, slots=True)
class Cmp:
    field: str
    op: str
    value: Value


Node = Union[Or, And, Not, Cmp]
