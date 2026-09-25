"""Rule model, validation and load-time conflict detection."""

import json

from .conditions import OPERATORS
from .errors import RuleConflictError, RuleValidationError

EFFECTS = ("allow", "deny")
GROUPS = ("subject", "resource", "action", "environment")


class Rule:
    __slots__ = ("rule_id", "effect", "priority", "conditions", "action_keys",
                 "rtype_keys")

    def __init__(self, rule_id, effect, priority, conditions, action_keys, rtype_keys):
        self.rule_id = rule_id
        self.effect = effect
        self.priority = priority
        self.conditions = conditions
        self.action_keys = action_keys  # tuple of actions, or ("*",)
        self.rtype_keys = rtype_keys    # tuple of resource types, or ("*",)

    def __repr__(self):
        return "Rule(%r, %s, p=%d)" % (self.rule_id, self.effect, self.priority)


def _validate_spec(spec, path):
    if isinstance(spec, dict) and "op" in spec:
        op = spec["op"]
        if op not in OPERATORS:
            raise RuleValidationError("%s: unknown operator %r" % (path, op))
        if op == "between":
            arg = spec.get("value")
            if not (isinstance(arg, list) and len(arg) == 2):
                raise RuleValidationError("%s: between needs [lo, hi]" % path)
        elif op == "in":
            if not isinstance(spec.get("value"), list):
                raise RuleValidationError("%s: in needs a list value" % path)
        elif op != "exists" and "value" not in spec:
            raise RuleValidationError("%s: operator %s needs a value" % (path, op))


def _keys_from_spec(spec):
    """Index keys for a spec: concrete strings, or None when not indexable."""
    if isinstance(spec, str):
        return (spec,)
    if isinstance(spec, list) and spec and all(isinstance(v, str) for v in spec):
        return tuple(spec)
    return None


def parse_rule(raw):
    if not isinstance(raw, dict):
        raise RuleValidationError("rule must be an object, got %r" % (raw,))
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise RuleValidationError("rule %s: id must be a non-empty string" % (raw,))
    effect = raw.get("effect")
    if effect not in EFFECTS:
        raise RuleValidationError("rule %s: effect must be allow|deny" % rule_id)
    priority = raw.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise RuleValidationError("rule %s: priority must be an integer" % rule_id)
    conditions = raw.get("conditions", {})
    if not isinstance(conditions, dict):
        raise RuleValidationError("rule %s: conditions must be an object" % rule_id)
    for group in conditions:
        if group not in GROUPS:
            raise RuleValidationError(
                "rule %s: unknown condition group %r" % (rule_id, group))

    action_keys = ("*",)
    rtype_keys = ("*",)
    for group, attrs in conditions.items():
        if group == "action":
            _validate_spec(attrs, "%s.action" % rule_id)
            keys = _keys_from_spec(attrs)
            if keys:
                action_keys = keys
            continue
        if not isinstance(attrs, dict):
            raise RuleValidationError(
                "rule %s: conditions.%s must be an object" % (rule_id, group))
        for attr, spec in attrs.items():
            _validate_spec(spec, "%s.%s.%s" % (rule_id, group, attr))
            if group == "resource" and attr == "type":
                keys = _keys_from_spec(spec)
                if keys:
                    rtype_keys = keys
    return Rule(rule_id, effect, priority, conditions, action_keys, rtype_keys)


def load_rules(data):
    """Parse and validate a list of raw rule dicts."""
    if not isinstance(data, list):
        raise RuleValidationError("rule set must be a JSON array")
    rules = []
    seen = set()
    for raw in data:
        rule = parse_rule(raw)
        if rule.rule_id in seen:
            raise RuleValidationError("duplicate rule id %r" % rule.rule_id)
        seen.add(rule.rule_id)
        rules.append(rule)
    return rules


def _normalize_spec(spec):
    """Canonical form so that [a, b] and [b, a] compare equal."""
    if isinstance(spec, list):
        return sorted(json.dumps(v, sort_keys=True) for v in spec)
    if isinstance(spec, dict):
        if spec.get("op") == "in" and isinstance(spec.get("value"), list):
            spec = dict(spec)
            spec["value"] = sorted(json.dumps(v, sort_keys=True) for v in spec["value"])
        return {k: _normalize_spec(v) for k, v in sorted(spec.items())}
    return spec


def _canonical_conditions(rule):
    return json.dumps(_normalize_spec(rule.conditions), sort_keys=True)


def detect_conflicts(rules):
    """Raise RuleConflictError if identical condition sets have both effects."""
    by_conditions = {}
    for rule in rules:
        key = _canonical_conditions(rule)
        by_conditions.setdefault(key, {}).setdefault(rule.effect, []).append(rule.rule_id)
    conflicts = []
    for key, effects in by_conditions.items():
        if len(effects) > 1:
            ids = sorted(i for ids in effects.values() for i in ids)
            conflicts.append({"conditions": key, "rule_ids": ids})
    if conflicts:
        raise RuleConflictError(conflicts)
