"""Concrete syntax tree emitted by stage 2.

Every node is an immutable ``NamedTuple``: stages hand values around but can
never mutate a node produced by an earlier stage.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Union


class Ref(NamedTuple):
    name: str
    offset: int


class Lit(NamedTuple):
    value: Union[str, int]
    offset: int


class Cmp(NamedTuple):
    op: str
    left: "Expr"
    right: "Expr"
    offset: int


class And(NamedTuple):
    left: "Expr"
    right: "Expr"
    offset: int


class Or(NamedTuple):
    left: "Expr"
    right: "Expr"
    offset: int


Expr = Union[Ref, Lit, Cmp, And, Or]


class Limit(NamedTuple):
    value: int
    offset: int


class Program(NamedTuple):
    select: tuple[Ref, ...]
    table: Ref
    where: Optional[Expr]
    limit: Optional[Limit]
