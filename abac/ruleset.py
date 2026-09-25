"""Rule model, load-time validation, conflict detection and indexing."""

import json
import re

from .conditions import (
    MISSING,
    AttributeMissingError,
    apply_op,
    kind_of,
    lookup,
    validate_arg,
)

EFFECTS = ("allow", "deny")

# Attribute paths preferred as index keys, most selective first.
INDEX_PATH_ORDER = (
    "action",
    "resource.type",
    "resource.id",
    "subject.id",
    "subject.role",
    "resource.owner",
)

_HASHABLE_KINDS = ("bool", "number", "string")


class RuleSetError(Exception):
    """Raised at load time for invalid or contradictory rule sets."""


class Ref(object):
    """A value reference to another request attribute, e.g. "$subject.id"."""

    __slots__ = ("path",)

    def __init__(self, path):
        self.path = path

    def __repr__(self):
        return "$" + self.path


def _canon_value(value):
    if isinstance(value, Ref):
        return ("ref", value.path)
    if isinstance(value, re.Pattern):
        return ("regex", value.pattern)
    if isinstance(value, list):
        return tuple(sorted(repr(v) for v in value))
    return ("const", kind_of(value), repr(value))


class Condition(object):
    __slots__ = ("path", "op", "arg")

    def __init__(self, path, op, arg):
        self.path = path
        self.op = op
        self.arg = arg

    def canonical(self):
        return (self.path, self.op, _canon_value(self.arg))

    def describe_failure(self, actual):
        return "%s: expected %s %r, got %r" % (self.path, self.op, self.arg, actual)


def _parse_arg(op, arg, path):
    if isinstance(arg, str) and arg.startswith("$"):
        return Ref(arg[1:])
    return validate_arg(op, arg, path)


def _parse_attr_conditions(path, raw):
    if isinstance(raw, dict):
        if not raw:
            raise ValueError("empty condition object at %s" % path)
        conditions = []
        for op in sorted(raw):
            if op not in _OPS_FOR_PARSE:
                raise ValueError("unknown operator %r at %s" % (op, path))
            conditions.append(Condition(path, op, _parse_arg(op, raw[op], path)))
        return conditions
    return [Condition(path, "eq", _parse_arg("eq", raw, path))]


_OPS_FOR_PARSE = frozenset(
    [
        "eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte",
        "between", "contains", "prefix", "suffix", "matches", "exists",
    ]
)


class Rule(object):
    def __init__(self, raw):
        if not isinstance(raw, dict):
            raise RuleSetError("rule must be an object, got %r" % (raw,))
        self.id = raw.get("id")
        if not self.id or not isinstance(self.id, str):
            raise RuleSetError("every rule needs a non-empty string 'id'")
        self.effect = raw.get("effect")
        if self.effect not in EFFECTS:
            raise RuleSetError("rule %r: effect must be one of %s" % (self.id, EFFECTS))
        self.priority = raw.get("priority", 0)
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise RuleSetError("rule %r: priority must be an integer" % self.id)
        when = raw.get("when")
        if not isinstance(when, dict) or not when:
            raise RuleSetError("rule %r: 'when' must be a non-empty object" % self.id)
        self.conditions = []
        for section in ("subject", "resource", "env"):
            attrs = when.get(section)
            if attrs is None:
                continue
            if not isinstance(attrs, dict):
                raise RuleSetError(
                    "rule %r: when.%s must be an object" % (self.id, section)
                )
            for attr in sorted(attrs):
                self.conditions.extend(
                    _parse_attr_conditions("%s.%s" % (section, attr), attrs[attr])
                )
        if "action" in when:
            self.conditions.extend(_parse_attr_conditions("action", when["action"]))
        if not self.conditions:
            raise RuleSetError("rule %r: no conditions parsed" % self.id)

    def canonical_conditions(self):
        return tuple(sorted(c.canonical() for c in self.conditions))

    def index_key(self):
        """Pick the most selective constant equality condition as index key."""
        best = None
        for cond in self.conditions:
            if cond.op != "eq" or isinstance(cond.arg, Ref):
                continue
            if kind_of(cond.arg) not in _HASHABLE_KINDS:
                continue
            try:
                rank = INDEX_PATH_ORDER.index(cond.path)
            except ValueError:
                rank = len(INDEX_PATH_ORDER)
            candidate = (rank, cond.path, cond.arg)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            return None
        return (best[1], best[2])

    def matches(self, request):
        """Return (matched, failure_reason). Raises on missing/typed errors."""
        for cond in self.conditions:
            expected = cond.arg
            if isinstance(expected, Ref):
                expected = lookup(request, expected.path)
                if expected is MISSING:
                    raise AttributeMissingError(expected.path)
            actual = lookup(request, cond.path)
            if not apply_op(cond.op, actual, expected, cond.path):
                return False, cond.describe_failure(actual)
        return True, None


class RuleSet(object):
    """A validated, conflict-checked, indexed set of rules."""

    def __init__(self, rules_raw, config=None):
        config = config or {}
        self.on_attribute_error = config.get("on_attribute_error", "error")
        if self.on_attribute_error not in ("error", "deny"):
            raise RuleSetError(
                "config.on_attribute_error must be 'error' or 'deny'"
            )
        self.default_effect = config.get("default_effect", "deny")
        if self.default_effect not in EFFECTS:
            raise RuleSetError("config.default_effect must be 'allow' or 'deny'")

        self.rules = [Rule(r) for r in rules_raw]
        self._check_unique_ids()
        self._check_contradictions()
        self._build_index()

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            raise RuleSetError("rule set document must be an object with a 'rules' list")
        return cls(data["rules"], data.get("config"))

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def _check_unique_ids(self):
        seen = set()
        for rule in self.rules:
            if rule.id in seen:
                raise RuleSetError("duplicate rule id: %r" % rule.id)
            seen.add(rule.id)

    def _check_contradictions(self):
        """Same conditions + same priority + opposite effects => load error.

        Different priorities are resolvable (higher wins) and same-effect
        duplicates are harmless, so only the truly ambiguous case is rejected.
        """
        by_shape = {}
        for rule in self.rules:
            key = (rule.canonical_conditions(), rule.priority)
            other = by_shape.get(key)
            if other is not None and other.effect != rule.effect:
                raise RuleSetError(
                    "contradictory rules: %r (%s) and %r (%s) have identical "
                    "conditions and priority %d but opposite effects"
                    % (other.id, other.effect, rule.id, rule.effect, rule.priority)
                )
            by_shape.setdefault(key, rule)

    def _build_index(self):
        self._buckets = {}  # path -> {value: [Rule]}
        self._always = []   # rules without a constant equality condition
        for rule in self.rules:
            key = rule.index_key()
            if key is None:
                self._always.append(rule)
                continue
            path, value = key
            self._buckets.setdefault(path, {}).setdefault(value, []).append(rule)

    def candidates(self, request, use_index=True):
        """Rules that can possibly match, in O(#indexed paths)."""
        if not use_index:
            return list(self.rules)
        result = list(self._always)
        for path, buckets in self._buckets.items():
            actual = lookup(request, path)
            if actual is MISSING or kind_of(actual) not in _HASHABLE_KINDS:
                continue
            hit = buckets.get(actual)
            if hit:
                result.extend(hit)
        return result
