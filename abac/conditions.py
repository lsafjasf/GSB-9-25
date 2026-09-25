"""Attribute condition matching.

A condition spec for one attribute can be:

  scalar                        -> strict equality (type must match)
  [v1, v2, ...]                 -> membership ("in")
  {"op": OP, "value": ARG}      -> operator form, OP in:
      eq, ne, in, lt, le, gt, ge, between, exists

Missing attributes and incompatible types never silently evaluate to a
match: they raise AttributeProblem and the engine applies the configured
missing-attribute policy.
"""

MISSING = object()

OPERATORS = ("eq", "ne", "in", "lt", "le", "gt", "ge", "between", "exists")


class AttributeProblem(Exception):
    """An attribute is missing from the request or has an incompatible type."""

    def __init__(self, path, kind, detail):
        super().__init__("%s: %s: %s" % (path, kind, detail))
        self.path = path
        self.kind = kind  # "missing" | "type"
        self.detail = detail


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compatible(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if _is_number(a) or _is_number(b):
        return _is_number(a) and _is_number(b)
    return isinstance(a, str) and isinstance(b, str)


def _strict_eq(a, b):
    return _compatible(a, b) and a == b


def _compare(a, b):
    """Return -1/0/1, or None when the types are not comparable."""
    if not _compatible(a, b):
        return None
    if isinstance(a, bool):
        return None
    return (a > b) - (a < b)


def _type_name(value):
    if value is MISSING:
        return "<missing>"
    if isinstance(value, bool):
        return "bool"
    if _is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _match_op(op, arg, value, path):
    if op == "eq" or op == "ne":
        if not _compatible(arg, value):
            raise AttributeProblem(
                path, "type",
                "cannot compare %s with %s" % (_type_name(arg), _type_name(value)))
        return (arg == value) if op == "eq" else (arg != value)
    if op == "in":
        # Membership never raises on type mismatch: a value of another type
        # simply is not a member.
        return any(_strict_eq(item, value) for item in arg)
    if op in ("lt", "le", "gt", "ge"):
        cmp = _compare(value, arg)
        if cmp is None:
            raise AttributeProblem(
                path, "type",
                "operator %s needs two numbers or two strings, got %s and %s"
                % (op, _type_name(value), _type_name(arg)))
        return {"lt": cmp < 0, "le": cmp <= 0, "gt": cmp > 0, "ge": cmp >= 0}[op]
    if op == "between":
        lo, hi = arg
        c_lo = _compare(value, lo)
        c_hi = _compare(value, hi)
        if c_lo is None or c_hi is None:
            raise AttributeProblem(
                path, "type",
                "between needs numbers or strings, got %s in [%s, %s]"
                % (_type_name(value), _type_name(lo), _type_name(hi)))
        return c_lo >= 0 and c_hi <= 0
    raise AttributeProblem(path, "type", "unknown operator %r" % (op,))


def match_spec(spec, value, path):
    """Match one attribute value against its spec.

    Returns True/False. Raises AttributeProblem on missing attributes or
    incompatible types.
    """
    if isinstance(spec, dict) and "op" in spec:
        op = spec["op"]
        if op == "exists":
            return (value is not MISSING) == bool(spec.get("value", True))
        if value is MISSING:
            raise AttributeProblem(path, "missing", "attribute not present in request")
        return _match_op(op, spec.get("value"), value, path)
    if value is MISSING:
        raise AttributeProblem(path, "missing", "attribute not present in request")
    if isinstance(spec, list):
        return any(_strict_eq(item, value) for item in spec)
    if not _compatible(spec, value):
        raise AttributeProblem(
            path, "type",
            "rule expects %s, request has %s" % (_type_name(spec), _type_name(value)))
    return spec == value
