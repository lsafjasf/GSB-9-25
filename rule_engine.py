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


class ReplayMismatchError(Exception):
    """Raised when re-running the same facts does not reproduce a trace.

    Carries ``diffs``: the sorted list of top-level trace keys that diverged.
    """

    def __init__(self, diffs):
        self.diffs = list(diffs)
        super().__init__("replay diverged at: " + ", ".join(self.diffs))


_OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def _trace_condition(cond, facts):
    """Evaluate a condition and return a JSON-serializable explanation tree.

    Never raises for condition problems: failures are recorded on the node
    as ``error_type``/``error`` and the node's ``result`` stays None.
    Short-circuited clauses appear as ``unevaluated`` placeholder nodes so
    every declared item shows up in the trace.
    """
    if not isinstance(cond, dict):
        return {"type": "error", "result": None, "error_type": "condition_error",
                "error": "condition must be a dict, got %s" % type(cond).__name__}
    if "and" in cond:
        return _trace_logic("and", cond["and"], facts)
    if "or" in cond:
        return _trace_logic("or", cond["or"], facts)
    if "not" in cond:
        child = _trace_condition(cond["not"], facts)
        result = None if _first_error(child) is not None else not child["result"]
        return {"type": "not", "result": result, "child": child}
    return _trace_leaf(cond, facts)


def _trace_logic(kind, clauses, facts):
    node = {"type": kind, "result": None, "children": []}
    stopped = False
    for clause in clauses:
        if stopped:
            node["children"].append({"type": "unevaluated", "result": None,
                                     "reason": "short_circuit",
                                     "condition": clause})
            continue
        child = _trace_condition(clause, facts)
        node["children"].append(child)
        if _first_error(child) is not None:
            stopped = True  # mirrors exception propagation in eager evaluation
        elif kind == "and" and not child["result"]:
            node["result"] = False
            stopped = True
        elif kind == "or" and child["result"]:
            node["result"] = True
            stopped = True
    if node["result"] is None and not stopped:
        results = [child["result"] for child in node["children"]]
        node["result"] = all(results) if kind == "and" else any(results)
    return node


def _trace_leaf(cond, facts):
    try:
        field_name = cond["field"]
        op = cond["op"]
    except KeyError as exc:
        return {"type": "error", "result": None, "error_type": "condition_error",
                "error": "malformed condition, missing key %s" % exc}
    node = {"type": "cmp", "field": field_name, "op": op,
            "expected": cond.get("value"), "result": None}
    if field_name not in facts:
        node.update(error_type="field_missing", error=str(field_name))
        return node
    actual = facts[field_name]
    node["actual"] = actual
    if op in ("in", "not_in"):
        members = cond.get("value")
        if not isinstance(members, (list, tuple, set, frozenset)):
            node.update(error_type="condition_error",
                        error="'%s' expects a collection, got %s"
                              % (op, type(members).__name__))
            return node
        node["result"] = (actual in members) if op == "in" else (actual not in members)
        return node
    func = _OPS.get(op)
    if func is None:
        node.update(error_type="condition_error",
                    error="unknown operator %r" % (op,))
        return node
    try:
        node["result"] = bool(func(actual, cond.get("value")))
    except TypeError as exc:
        node.update(error_type="condition_error",
                    error="cannot compare %s=%r with %r: %s"
                          % (field_name, actual, cond.get("value"), exc))
    return node


def _first_error(node):
    """Return the first error node in the tree (pre-order), or None."""
    if "error" in node:
        return node
    for child in node.get("children", ()):
        err = _first_error(child)
        if err is not None:
            return err
    child = node.get("child")
    if child is not None:
        return _first_error(child)
    return None


def evaluate_condition(cond, facts):
    """Evaluate a condition dict against facts. Pure; never mutates facts."""
    node = _trace_condition(cond, facts)
    err = _first_error(node)
    if err is not None:
        if err["error_type"] == "field_missing":
            raise FieldMissingError(err["error"])
        raise ConditionError(err["error"])
    return node["result"]


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
            "field_sources": None,
            "final_state": None,
            "final_flags": None,
        }

        matched = []
        for order, rule in enumerate(self._rules):
            entry = {"rule_id": rule.rule_id, "order": order,
                     "priority": rule.priority}
            node = _trace_condition(rule.condition, facts)
            entry["condition"] = node
            err = _first_error(node)
            if err is not None:
                entry.update(result="skipped",
                             reason="%s: %s" % (err["error_type"], err["error"]))
            elif node["result"]:
                entry["result"] = "matched"
                matched.append((order, rule))
            else:
                entry.update(result="skipped", reason="condition_false")
            trace["evaluations"].append(entry)

        # Execution order: priority desc, then declaration order asc.
        matched.sort(key=lambda pair: (-pair[1].priority, pair[0]))
        trace["matched"] = [rule.rule_id for _, rule in matched]

        writers = {}       # field -> (rule_id, value, priority, order) of winner
        last_writer = {}   # field -> (rule_id, action_index) of last applied set
        applied_sets = {}  # field -> [action_index, ...] of applied set actions
        for order, rule in matched:
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
                    prev_rule, prev_value, prev_prio, prev_order = writers[field_name]
                    if prev_rule != rule.rule_id and prev_value != value:
                        conflict = {
                            "field": field_name,
                            "strategy": self.conflict_strategy,
                            "kept": {"rule_id": prev_rule, "value": prev_value},
                            "rejected": {"rule_id": rule.rule_id, "value": value},
                            "basis": _conflict_basis(
                                prev_rule, prev_prio, prev_order,
                                rule.rule_id, rule.priority, order),
                        }
                        trace["conflicts"].append(conflict)
                        trace["actions"].append({
                            "rule_id": rule.rule_id, "type": "set",
                            "field": field_name, "old": prev_value, "new": value,
                            "applied": False, "reason": "conflict",
                            "overridden_by": prev_rule,
                        })
                        if self.conflict_strategy == "reject":
                            _finalize_trace(trace, state, flags,
                                            last_writer, applied_sets)
                            raise ConflictError(trace["conflicts"], trace)
                        continue
                    # Same value from another rule: idempotent, not a conflict.
                existed = field_name in state
                old = state.get(field_name)
                state[field_name] = value
                action_index = len(trace["actions"])
                writers[field_name] = (rule.rule_id, value, rule.priority, order)
                last_writer[field_name] = (rule.rule_id, action_index)
                applied_sets.setdefault(field_name, []).append(action_index)
                trace["actions"].append({
                    "rule_id": rule.rule_id, "type": "set",
                    "field": field_name, "old": old, "existed": existed,
                    "new": value, "applied": True,
                })

        _finalize_trace(trace, state, flags, last_writer, applied_sets)
        return RunResult(state, flags, trace)

    def replay(self, facts, trace):
        """Re-run ``facts`` and verify the recorded ``trace`` is reproduced.

        The trace is deterministic, so a faithful replay must regenerate it
        exactly (same conclusion, same explanation). Returns the RunResult,
        or None when the recorded decision itself raised ConflictError under
        the 'reject' strategy. Raises ReplayMismatchError otherwise.
        """
        result = None
        try:
            result = self.run(facts)
            candidate = result.trace
        except ConflictError as exc:
            candidate = exc.trace
        if candidate != trace:
            keys = set(candidate) | set(trace)
            diffs = sorted(k for k in keys if candidate.get(k) != trace.get(k))
            raise ReplayMismatchError(diffs)
        return result


def _conflict_basis(kept_id, kept_prio, kept_order, rej_id, rej_prio, rej_order):
    """Human-readable justification for why the kept writer wins."""
    if kept_prio != rej_prio:
        return ("priority %d > %d: rule %r executes before %r, "
                "first writer keeps the field"
                % (kept_prio, rej_prio, kept_id, rej_id))
    return ("priority tie at %d: rule %r declared earlier (order %d < %d), "
            "first writer keeps the field"
            % (kept_prio, kept_id, kept_order, rej_order))


def _finalize_trace(trace, state, flags, last_writer, applied_sets):
    """Mark superseded actions and record the source of every final field."""
    for field_name, indices in applied_sets.items():
        final_rule = last_writer[field_name][0]
        for action_index in indices[:-1]:
            trace["actions"][action_index]["superseded_by"] = final_rule
    sources = {}
    for field_name in state:
        if field_name in last_writer:
            src_rule, action_index = last_writer[field_name]
            sources[field_name] = {"source": src_rule,
                                   "action_index": action_index}
        else:
            sources[field_name] = {"source": "input"}
    trace["field_sources"] = sources
    trace["final_state"] = state
    trace["final_flags"] = sorted(flags)


def _format_condition(node, depth):
    pad = "  " * depth
    ntype = node["type"]
    if ntype == "cmp":
        if "error" in node:
            return ["%s%s %s %r -> ERROR %s: %s"
                    % (pad, node.get("field"), node.get("op"),
                       node.get("expected"), node["error_type"], node["error"])]
        return ["%s%s %s %r: actual=%r -> %s"
                % (pad, node["field"], node["op"], node.get("expected"),
                   node.get("actual"), node["result"])]
    if ntype == "error":
        return ["%sERROR %s: %s" % (pad, node["error_type"], node["error"])]
    if ntype == "unevaluated":
        return ["%s<unevaluated: %s> %r" % (pad, node["reason"], node["condition"])]
    if ntype == "not":
        lines = ["%snot -> %s" % (pad, node["result"])]
        lines.extend(_format_condition(node["child"], depth + 1))
        return lines
    lines = ["%s%s -> %s" % (pad, ntype, node["result"])]
    for child in node["children"]:
        lines.extend(_format_condition(child, depth + 1))
    return lines


def format_trace(trace):
    """Render a trace as a human-readable explanation (list of lines)."""
    lines = ["strategy: %s" % trace.get("strategy"), "== evaluations =="]
    for ev in trace["evaluations"]:
        head = "[%s] %s (order=%d, priority=%d)" % (
            ev["result"], ev["rule_id"], ev["order"], ev["priority"])
        if ev.get("reason"):
            head += " -- " + ev["reason"]
        lines.append(head)
        lines.extend(_format_condition(ev["condition"], 1))
    lines.append("== execution order ==")
    lines.append(", ".join(trace["matched"]) if trace["matched"] else "(none)")
    lines.append("== actions ==")
    if not trace["actions"]:
        lines.append("(none)")
    for i, act in enumerate(trace["actions"]):
        if act["type"] == "flag":
            lines.append("#%d [%s] flag %s (%s)"
                         % (i, act["rule_id"], act["flag"],
                            "new" if act["changed"] else "already set"))
        elif act.get("applied"):
            note = ""
            if "superseded_by" in act:
                note = " [superseded_by %s]" % act["superseded_by"]
            lines.append("#%d [%s] set %s: %r -> %r (applied)%s"
                         % (i, act["rule_id"], act["field"],
                            act.get("old"), act["new"], note))
        else:
            lines.append("#%d [%s] set %s: %r -> %r (REJECTED: %s, "
                         "overridden_by %s)"
                         % (i, act["rule_id"], act["field"], act.get("old"),
                            act["new"], act.get("reason"),
                            act.get("overridden_by")))
    lines.append("== conflicts ==")
    if not trace["conflicts"]:
        lines.append("(none)")
    for conflict in trace["conflicts"]:
        lines.append("[conflict] field %r: kept %s=%r, rejected %s=%r"
                     % (conflict["field"],
                        conflict["kept"]["rule_id"], conflict["kept"]["value"],
                        conflict["rejected"]["rule_id"],
                        conflict["rejected"]["value"]))
        lines.append("  basis: %s" % conflict.get("basis"))
    lines.append("== field sources ==")
    for field_name, src in (trace.get("field_sources") or {}).items():
        if src["source"] == "input":
            lines.append("  %s <= input" % field_name)
        else:
            lines.append("  %s <= rule %s (action #%d)"
                         % (field_name, src["source"], src["action_index"]))
    lines.append("== conclusion ==")
    lines.append("final_state: %r" % (trace.get("final_state"),))
    lines.append("final_flags: %s" % (trace.get("final_flags"),))
    return lines
