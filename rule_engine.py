"""A tiny deterministic rule engine (Python 3 standard library only).

Concepts
--------
- Facts:   an input dict. Never mutated by the engine.
- Rule:    id + condition (dict DSL) + action list + priority.
- Action:  {"set": field, "value": v}  -> assign a field in the output state
           {"flag": name}              -> add a marker to the output flag set
- Run:     conditions are evaluated in declaration order; matched rules then
           execute ordered by (priority desc, declaration order asc).
- Conflict: two different rules assign different values to the same field.
           Resolved by an explicit strategy ("priority" or "reject") and
           recorded in the trace.

Condition DSL
-------------
    {"field": "age", "op": ">=", "value": 18}
    {"field": "tier", "op": "in", "value": ["gold", "silver"]}
    {"and": [cond, ...]}  {"or": [cond, ...]}  {"not": cond}
Operators: == != < <= > >= in not_in
"""

from __future__ import annotations

import operator


class FieldMissingError(Exception):
    """A condition referenced a field absent from the facts."""


class ConditionError(Exception):
    """A condition could not be evaluated (bad shape or incompatible types)."""


class ConflictError(Exception):
    """Raised under the 'reject' strategy when rules assign conflicting values.

    Carries the list of conflicts and the trace captured up to the failure.
    """

    def __init__(self, conflicts, trace):
        self.conflicts = conflicts
        self.trace = trace
        detail = "; ".join(
            "field %r: %s=%r vs %s=%r"
            % (c["field"], c["kept"]["rule_id"], c["kept"]["value"],
               c["rejected"]["rule_id"], c["rejected"]["value"])
            for c in conflicts
        )
        super().__init__("conflicting assignments rejected: " + detail)


_OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def evaluate_condition(cond, facts):
    """Evaluate a condition dict against facts. Pure; never mutates facts."""
    if not isinstance(cond, dict):
        raise ConditionError("condition must be a dict, got %s" % type(cond).__name__)
    if "and" in cond:
        return all(evaluate_condition(c, facts) for c in cond["and"])
    if "or" in cond:
        return any(evaluate_condition(c, facts) for c in cond["or"])
    if "not" in cond:
        return not evaluate_condition(cond["not"], facts)
    try:
        field_name = cond["field"]
        op = cond["op"]
    except KeyError as exc:
        raise ConditionError("malformed condition, missing key %s" % exc) from None
    if field_name not in facts:
        raise FieldMissingError(field_name)
    actual = facts[field_name]
    if op in ("in", "not_in"):
        members = cond.get("value")
        if not isinstance(members, (list, tuple, set, frozenset)):
            raise ConditionError(
                "'%s' expects a collection, got %s" % (op, type(members).__name__))
        return (actual in members) if op == "in" else (actual not in members)
    func = _OPS.get(op)
    if func is None:
        raise ConditionError("unknown operator %r" % (op,))
    try:
        return bool(func(actual, cond.get("value")))
    except TypeError as exc:
        raise ConditionError(
            "cannot compare %s=%r with %r: %s"
            % (field_name, actual, cond.get("value"), exc)) from None


class Rule:
    __slots__ = ("rule_id", "condition", "actions", "priority")

    def __init__(self, rule_id, condition, actions, priority=0):
        if not rule_id:
            raise ValueError("rule_id must be a non-empty identifier")
        if not isinstance(actions, (list, tuple)) or not actions:
            raise ValueError("rule %r must declare at least one action" % rule_id)
        for action in actions:
            self._validate_action(rule_id, action)
        self.rule_id = rule_id
        self.condition = condition
        self.actions = list(actions)
        self.priority = priority

    @staticmethod
    def _validate_action(rule_id, action):
        if not isinstance(action, dict):
            raise ValueError("rule %r: action must be a dict" % rule_id)
        if "flag" in action:
            if not action["flag"]:
                raise ValueError("rule %r: flag name must be non-empty" % rule_id)
        elif "set" in action:
            if not action["set"]:
                raise ValueError("rule %r: set field must be non-empty" % rule_id)
            if "value" not in action:
                raise ValueError("rule %r: set action requires 'value'" % rule_id)
        else:
            raise ValueError(
                "rule %r: action must contain 'set' or 'flag'" % rule_id)


class RunResult:
    __slots__ = ("state", "flags", "trace")

    def __init__(self, state, flags, trace):
        self.state = state
        self.flags = flags
        self.trace = trace


class RuleEngine:
    """An ordered collection of rules with explicit conflict resolution."""

    STRATEGIES = ("priority", "reject")

    def __init__(self, conflict_strategy="priority"):
        if conflict_strategy not in self.STRATEGIES:
            raise ValueError(
                "conflict_strategy must be one of %r" % (self.STRATEGIES,))
        self.conflict_strategy = conflict_strategy
        self._rules = []  # declaration order is significant

    def add_rule(self, rule):
        """Append a rule. Raises ValueError on duplicate rule_id."""
        if any(r.rule_id == rule.rule_id for r in self._rules):
            raise ValueError("duplicate rule_id %r" % rule.rule_id)
        self._rules.append(rule)
        return rule

    def remove_rule(self, rule_id):
        """Remove a rule by id. Returns True if a rule was removed."""
        for i, rule in enumerate(self._rules):
            if rule.rule_id == rule_id:
                del self._rules[i]
                return True
        return False

    @property
    def rules(self):
        return list(self._rules)

    def run(self, facts):
        """Evaluate all rules against facts and return a RunResult.

        The input facts dict is never mutated. The trace is deterministic:
        the same engine state and facts always produce an identical trace.
        """
        state = dict(facts)
        flags = set()
        trace = {
            "strategy": self.conflict_strategy,
            "evaluations": [],
            "matched": [],
            "actions": [],
            "conflicts": [],
            "final_state": None,
            "final_flags": None,
        }

        matched = []
        for order, rule in enumerate(self._rules):
            entry = {"rule_id": rule.rule_id, "order": order,
                     "priority": rule.priority}
            try:
                hit = evaluate_condition(rule.condition, facts)
            except FieldMissingError as exc:
                entry.update(result="skipped", reason="field_missing: %s" % exc)
            except ConditionError as exc:
                entry.update(result="skipped", reason="condition_error: %s" % exc)
            else:
                if hit:
                    entry["result"] = "matched"
                    matched.append((order, rule))
                else:
                    entry.update(result="skipped", reason="condition_false")
            trace["evaluations"].append(entry)

        # Execution order: priority desc, then declaration order asc.
        matched.sort(key=lambda pair: (-pair[1].priority, pair[0]))
        trace["matched"] = [rule.rule_id for _, rule in matched]

        writers = {}  # field -> (rule_id, value) of the first (winning) writer
        for _, rule in matched:
            for action in rule.actions:
                if "flag" in action:
                    name = action["flag"]
                    added = name not in flags
                    flags.add(name)
                    trace["actions"].append({
                        "rule_id": rule.rule_id, "type": "flag",
                        "flag": name, "changed": added,
                    })
                    continue
                field_name = action["set"]
                value = action["value"]
                if field_name in writers:
                    prev_rule, prev_value = writers[field_name]
                    if prev_rule != rule.rule_id and prev_value != value:
                        conflict = {
                            "field": field_name,
                            "strategy": self.conflict_strategy,
                            "kept": {"rule_id": prev_rule, "value": prev_value},
                            "rejected": {"rule_id": rule.rule_id, "value": value},
                        }
                        trace["conflicts"].append(conflict)
                        trace["actions"].append({
                            "rule_id": rule.rule_id, "type": "set",
                            "field": field_name, "old": prev_value, "new": value,
                            "applied": False, "reason": "conflict",
                        })
                        if self.conflict_strategy == "reject":
                            trace["final_state"] = state
                            trace["final_flags"] = sorted(flags)
                            raise ConflictError(trace["conflicts"], trace)
                        continue
                    # Same value from another rule: idempotent, not a conflict.
                existed = field_name in state
                old = state.get(field_name)
                state[field_name] = value
                writers[field_name] = (rule.rule_id, value)
                trace["actions"].append({
                    "rule_id": rule.rule_id, "type": "set",
                    "field": field_name, "old": old, "existed": existed,
                    "new": value, "applied": True,
                })

        trace["final_state"] = state
        trace["final_flags"] = sorted(flags)
        return RunResult(state, flags, trace)
