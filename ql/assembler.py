"""Stage 3: result assembly and semantic validation (CST -> result dict).

Input  : an immutable :class:`ql.cst.Program`.
Output : the public ``{"ok": True, "query": ...}`` payload.

Semantic checks (duplicate columns, constant WHERE, LIMIT range) happen here;
structural questions are already answered by stage 2, so the assembler never
looks at tokens or source text.
"""

from __future__ import annotations

from typing import Iterable

from . import cst
from .cst import And, Cmp, Limit, Lit, Or, Program, Ref
from .errors import duplicate_column, limit_range, where_constant


def assemble(program: Program) -> dict:
    result = {
        "select": [ref.name for ref in program.select],
        "from": program.table.name,
        "where": None,
        "limit": None,
    }

    _check_duplicate_columns(program.select)

    if program.where is not None:
        if not _references_column(program.where):
            raise where_constant(program.where.offset)
        result["where"] = _emit_expr(program.where)

    if program.limit is not None:
        limit = program.limit
        if not 0 <= limit.value <= 1000:
            raise limit_range(limit.offset, limit.value)
        result["limit"] = limit.value

    return {"ok": True, "query": result}


def _check_duplicate_columns(columns: Iterable[Ref]) -> None:
    seen: set[str] = set()
    for ref in columns:
        if ref.name in seen:
            raise duplicate_column(ref.offset, ref.name)
        seen.add(ref.name)


def _references_column(expr: cst.Expr) -> bool:
    if isinstance(expr, Ref):
        return True
    if isinstance(expr, Lit):
        return False
    return _references_column(expr.left) or _references_column(expr.right)


def _emit_expr(expr: cst.Expr) -> dict:
    if isinstance(expr, Ref):
        return {"type": "ref", "name": expr.name}
    if isinstance(expr, Lit):
        return {"type": "literal", "value": expr.value}
    if isinstance(expr, Cmp):
        return {
            "type": "cmp",
            "op": expr.op,
            "left": _emit_expr(expr.left),
            "right": _emit_expr(expr.right),
        }
    # Flatten same-operator chains: (a AND b) AND c -> ["and", a, b, c].
    op = "and" if isinstance(expr, And) else "or"
    operands = []
    for child in (expr.left, expr.right):
        emitted = _emit_expr(child)
        if emitted.get("type") == op:
            operands.extend(emitted["args"])
        else:
            operands.append(emitted)
    return {"type": op, "args": operands}
