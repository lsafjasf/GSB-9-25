"""Condition operators for ABAC rule evaluation.

Standard library only. Every operator is total and deterministic: when an
attribute is missing or has an incompatible type, an explicit, distinguishable
error is raised instead of silently evaluating to False.
"""

import re


class _Missing(object):
    def __repr__(self):
        return "<MISSING>"


MISSING = _Missing()


class AttributeMissingError(Exception):
    """A condition referenced an attribute that is absent from the request."""

    def __init__(self, path):
        self.path = path
        super(AttributeMissingError, self).__init__("missing attribute: %s" % path)


class ConditionTypeError(Exception):
    """An attribute value has a type incompatible with the operator."""

    def __init__(self, path, op, expected, actual):
        self.path = path
        self.op = op
        super(ConditionTypeError, self).__init__(
            "type mismatch at %s: operator %r expects %s, got %s (%r)"
            % (path, op, expected, kind_of(actual), actual)
        )


def kind_of(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def lookup(request, path):
    """Resolve a dotted attribute path against the request context."""
    node = request
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node


def _op_eq(actual, expected, path):
    if kind_of(actual) != kind_of(expected):
        raise ConditionTypeError(path, "eq", kind_of(expected), actual)
    return actual == expected


def _op_ne(actual, expected, path):
    return not _op_eq(actual, expected, path)


def _op_in(actual, expected, path):
    return any(_safe_eq(actual, item) for item in expected)


def _op_not_in(actual, expected, path):
    return not _op_in(actual, expected, path)


def _safe_eq(a, b):
    if kind_of(a) != kind_of(b):
        return False
    return a == b


def _make_ordered(op, fn):
    def check(actual, expected, path):
        if kind_of(actual) != "number" or kind_of(expected) != "number":
            raise ConditionTypeError(path, op, "number", actual)
        return fn(actual, expected)

    check.__name__ = "_op_" + op
    return check


def _op_between(actual, expected, path):
    if kind_of(actual) != "number":
        raise ConditionTypeError(path, "between", "number", actual)
    lo, hi = expected
    return lo <= actual <= hi


def _op_contains(actual, expected, path):
    if kind_of(actual) != "list":
        raise ConditionTypeError(path, "contains", "list", actual)
    return any(_safe_eq(item, expected) for item in actual)


def _op_prefix(actual, expected, path):
    if kind_of(actual) != "string" or kind_of(expected) != "string":
        raise ConditionTypeError(path, "prefix", "string", actual)
    return actual.startswith(expected)


def _op_suffix(actual, expected, path):
    if kind_of(actual) != "string" or kind_of(expected) != "string":
        raise ConditionTypeError(path, "suffix", "string", actual)
    return actual.endswith(expected)


def _op_matches(actual, expected, path):
    if kind_of(actual) != "string":
        raise ConditionTypeError(path, "matches", "string", actual)
    return expected.search(actual) is not None


OPERATORS = {
    "eq": _op_eq,
    "ne": _op_ne,
    "in": _op_in,
    "not_in": _op_not_in,
    "gt": _make_ordered("gt", lambda a, b: a > b),
    "gte": _make_ordered("gte", lambda a, b: a >= b),
    "lt": _make_ordered("lt", lambda a, b: a < b),
    "lte": _make_ordered("lte", lambda a, b: a <= b),
    "between": _op_between,
    "contains": _op_contains,
    "prefix": _op_prefix,
    "suffix": _op_suffix,
    "matches": _op_matches,
}

# Operators whose argument must be a list of scalars.
_LIST_ARG_OPS = {"in", "not_in"}


def validate_arg(op, arg, path):
    """Validate/normalize an operator argument at rule-build time."""
    if op not in OPERATORS and op != "exists":
        raise ValueError("unknown operator %r at %s" % (op, path))
    if op in _LIST_ARG_OPS:
        if not isinstance(arg, list) or not arg:
            raise ValueError("operator %r at %s requires a non-empty list" % (op, path))
    elif op == "between":
        if (
            not isinstance(arg, list)
            or len(arg) != 2
            or kind_of(arg[0]) != "number"
            or kind_of(arg[1]) != "number"
            or arg[0] > arg[1]
        ):
            raise ValueError("operator 'between' at %s requires [min, max] numbers" % path)
    elif op == "matches":
        if kind_of(arg) != "string":
            raise ValueError("operator 'matches' at %s requires a string pattern" % path)
        try:
            return re.compile(arg)
        except re.error as exc:
            raise ValueError("invalid regex at %s: %s" % (path, exc))
    return arg


def apply_op(op, actual, expected, path):
    """Apply one operator. Raises on missing attributes / type mismatches."""
    if op == "exists":
        return (actual is not MISSING) == bool(expected)
    if actual is MISSING:
        raise AttributeMissingError(path)
    return OPERATORS[op](actual, expected, path)
