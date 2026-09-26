"""Stage 3: assemble the structure tree into the public result dict.

``assemble(node) -> dict`` is a total, pure function over the node types
defined in ``nodes.py``.  It contains no grammar knowledge and no error
handling: by construction the tree is well-formed.
"""

from .nodes import And, Cmp, Not, Num, Or, Ref, Str


def assemble(node):
    if isinstance(node, Or):
        return {"type": "or", "left": assemble(node.left),
                "right": assemble(node.right)}
    if isinstance(node, And):
        return {"type": "and", "left": assemble(node.left),
                "right": assemble(node.right)}
    if isinstance(node, Not):
        return {"type": "not", "operand": assemble(node.operand)}
    if isinstance(node, Cmp):
        return {"type": "cmp", "field": node.field, "op": node.op,
                "value": _assemble_value(node.value)}
    raise TypeError("unknown node type: %r" % type(node).__name__)


def _assemble_value(value):
    if isinstance(value, Num):
        return value.value
    if isinstance(value, Str):
        return value.value
    if isinstance(value, Ref):
        return {"type": "ref", "name": value.name}
    raise TypeError("unknown value type: %r" % type(value).__name__)
