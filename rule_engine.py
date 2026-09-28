"""A small deterministic rule engine (standard library only).

Concepts
--------
- Facts: the input dict passed to ``Engine.run``.
- State: a working copy of the facts plus a set of flags. Actions mutate
  the state; conditions are evaluated against the current state, in rule
  execution order (priority desc, then declaration order asc).
- Rule: a condition expression plus a list of actions, with a priority.
- Conflict: two matched rules assign *different* values to the same field.
  Resolved by an explicit strategy and always recorded in the trace.

Conflict strategies
-------------------
- ``"priority"``: the assignment from the rule that runs first (highest
  priority, then earliest declaration) wins; later conflicting assignments
  are rejected and recorded.
- ``"reject"``: the run raises :class:`ConflictError` on the first
  conflicting assignment; nothing from the conflicting action is applied.

Every run produces a full trace: matched rules, skipped rules with reasons,
each state change (before/after), and how each conflict was resolved.
The trace is deterministic: the same rules and input always produce the
same trace.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

__all__ = [
    "Engine",
    "Rule",
    "Field",
    "And",
    "Or",
    "Not",
    "Set",
    "Flag",
    "RunResult",
    "ConflictError",
    "MissingFieldError",
    "ConditionError",
]


class MissingFieldError(Exception):
    """Raised when a condition references a field absent from the state."""


class ConditionError(Exception):
    """Raised when a condition cannot be evaluated (e.g. bad types)."""


class ConflictError(Exception):
    """Raised under the ``reject`` strategy when rules assign different
    values to the same field."""

    def __init__(self, conflicts: Sequence["ConflictRecord"]):
        self.conflicts = list(conflicts)
        details = "; ".join(c.describe() for c in self.conflicts)
        super().__init__(f"unresolved conflict(s): {details}")


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

class Condition:
    """Base class for condition expressions."""

    def evaluate(self, state: "State") -> bool:
        raise NotImplementedError

    def __and__(self, other: "Condition") -> "Condition":
        return And(self, other)

    def __or__(self, other: "Condition") -> "Condition":
        return Or(self, other)

    def __invert__(self) -> "Condition":
        return Not(self)


class Field(Condition):
    """A condition rooted at a state field, built via comparison helpers.

    Example: ``Field("age").ge(18) & Field("tier").in_(["gold", "vip"])``
    """

    def __init__(self, name: str):
        self.name = name

    def _get(self, state: "State") -> Any:
        if self.name not in state.values:
            raise MissingFieldError(f"field {self.name!r} is missing")
        return state.values[self.name]

    def _compare(self, op: str, other: Any, fn: Callable[[Any, Any], bool]) -> Condition:
        field_name = self.name

        class _Cmp(Condition):
            def evaluate(self, state: "State") -> bool:
                value = Field(field_name)._get(state)
                try:
                    return bool(fn(value, other))
                except TypeError as exc:
                    raise ConditionError(
                        f"cannot compare field {field_name!r} "
                        f"({value!r}) with {other!r}: {exc}"
                    ) from exc

            def __repr__(self) -> str:
                return f"Field({field_name!r}).{op}({other!r})"

        return _Cmp()

    def eq(self, other: Any) -> Condition:
        return self._compare("eq", other, lambda a, b: a == b)

    def ne(self, other: Any) -> Condition:
        return self._compare("ne", other, lambda a, b: a != b)

    def lt(self, other: Any) -> Condition:
        return self._compare("lt", other, lambda a, b: a < b)

    def le(self, other: Any) -> Condition:
        return self._compare("le", other, lambda a, b: a <= b)

    def gt(self, other: Any) -> Condition:
        return self._compare("gt", other, lambda a, b: a > b)

    def ge(self, other: Any) -> Condition:
        return self._compare("ge", other, lambda a, b: a >= b)

    def in_(self, options: Sequence[Any]) -> Condition:
        options = list(options)
        field_name = self.name

        class _In(Condition):
            def evaluate(self, state: "State") -> bool:
                value = Field(field_name)._get(state)
                return value in options

            def __repr__(self) -> str:
                return f"Field({field_name!r}).in_({options!r})"

        return _In()

    def not_in(self, options: Sequence[Any]) -> Condition:
        return ~self.in_(options)

    def __repr__(self) -> str:
        return f"Field({self.name!r})"


class And(Condition):
    def __init__(self, *conditions: Condition):
        self.conditions = list(conditions)

    def evaluate(self, state: "State") -> bool:
        return all(c.evaluate(state) for c in self.conditions)

    def __repr__(self) -> str:
        return f"And({', '.join(repr(c) for c in self.conditions)})"


class Or(Condition):
    def __init__(self, *conditions: Condition):
        self.conditions = list(conditions)

    def evaluate(self, state: "State") -> bool:
        return any(c.evaluate(state) for c in self.conditions)

    def __repr__(self) -> str:
        return f"Or({', '.join(repr(c) for c in self.conditions)})"


class Not(Condition):
    def __init__(self, condition: Condition):
        self.condition = condition

    def evaluate(self, state: "State") -> bool:
        return not self.condition.evaluate(state)

    def __repr__(self) -> str:
        return f"Not({self.condition!r})"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class Action:
    """Base class for actions. ``apply`` mutates the state and returns a
    human-readable description of the change for the trace."""

    def apply(self, state: "State") -> str:
        raise NotImplementedError


class Set(Action):
    """Assign ``value`` to ``field``."""

    def __init__(self, field: str, value: Any):
        self.field = field
        self.value = value

    def apply(self, state: "State") -> str:
        old = state.values.get(self.field, "<missing>")
        state.values[self.field] = self.value
        return f"set {self.field}: {old!r} -> {self.value!r}"

    def __repr__(self) -> str:
        return f"Set({self.field!r}, {self.value!r})"


class Flag(Action):
    """Add a marker to the state's flag set."""

    def __init__(self, name: str):
        self.name = name

    def apply(self, state: "State") -> str:
        already = self.name in state.flags
        state.flags.add(self.name)
        verb = "kept" if already else "added"
        return f"flag {self.name}: {verb}"

    def __repr__(self) -> str:
        return f"Flag({self.name!r})"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    name: str
    condition: Condition
    actions: List[Action]
    priority: int = 0


# ---------------------------------------------------------------------------
# Trace records
# ---------------------------------------------------------------------------

@dataclass
class ConflictRecord:
    field: str
    existing_value: Any
    existing_rule: str
    attempted_value: Any
    attempted_rule: str
    resolution: str

    def describe(self) -> str:
        return (
            f"field {self.field!r}: rule {self.attempted_rule!r} tried "
            f"{self.attempted_value!r}, but rule {self.existing_rule!r} "
            f"already set {self.existing_value!r} -> {self.resolution}"
        )


@dataclass
class RuleTrace:
    rule: str
    status: str  # "matched" | "skipped"
    reason: str = ""
    changes: List[str] = field(default_factory=list)


@dataclass
class Trace:
    entries: List[RuleTrace] = field(default_factory=list)
    conflicts: List[ConflictRecord] = field(default_factory=list)

    def render(self) -> str:
        lines: List[str] = []
        for entry in self.entries:
            lines.append(f"[{entry.status}] {entry.rule}")
            if entry.reason:
                lines.append(f"    reason: {entry.reason}")
            for change in entry.changes:
                lines.append(f"    change: {change}")
        for conflict in self.conflicts:
            lines.append(f"[conflict] {conflict.describe()}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass
class State:
    values: Dict[str, Any]
    flags: set = field(default_factory=set)

    def snapshot(self) -> "State":
        return State(dict(self.values), set(self.flags))


@dataclass
class RunResult:
    state: State
    trace: Trace

    def trace_text(self) -> str:
        return self.trace.render()


class Engine:
    """An ordered collection of rules with deterministic execution."""

    STRATEGIES = ("priority", "reject")

    def __init__(self, conflict_strategy: str = "priority"):
        if conflict_strategy not in self.STRATEGIES:
            raise ValueError(
                f"unknown conflict strategy {conflict_strategy!r}; "
                f"expected one of {self.STRATEGIES}"
            )
        self.conflict_strategy = conflict_strategy
        self._rules: Dict[int, tuple] = {}
        self._counter = itertools.count()

    # -- dynamic rule management -------------------------------------------

    def add_rule(self, rule: Rule) -> int:
        rule_id = next(self._counter)
        self._rules[rule_id] = (rule_id, rule)
        return rule_id

    def remove_rule(self, rule_id: int) -> bool:
        return self._rules.pop(rule_id, None) is not None

    def clear(self) -> None:
        self._rules.clear()

    @property
    def rules(self) -> List[Rule]:
        return [rule for _, rule in self._ordered()]

    def _ordered(self) -> List[tuple]:
        # priority desc, then declaration order asc -> fully deterministic
        return sorted(
            self._rules.values(),
            key=lambda item: (-item[1].priority, item[0]),
        )

    # -- execution ----------------------------------------------------------

    def run(self, facts: Dict[str, Any]) -> RunResult:
        state = State(dict(facts))
        trace = Trace()
        assignments: Dict[str, tuple] = {}  # field -> (value, rule id, rule name)

        for rule_id, rule in self._ordered():
            entry = RuleTrace(rule=rule.name, status="skipped")
            trace.entries.append(entry)
            try:
                matched = rule.condition.evaluate(state)
            except (MissingFieldError, ConditionError) as exc:
                entry.reason = f"condition error: {exc}"
                continue
            if not matched:
                entry.reason = "condition not satisfied"
                continue
            entry.status = "matched"
            for action in rule.actions:
                if isinstance(action, Set):
                    record = self._check_conflict(action, rule_id, rule, assignments)
                    if record is not None:
                        trace.conflicts.append(record)
                        if self.conflict_strategy == "reject":
                            raise ConflictError([record])
                        # "priority": keep the earlier assignment
                        entry.changes.append(
                            f"rejected set {action.field} "
                            f"(conflict, kept {assignments[action.field][0]!r})"
                        )
                        continue
                entry.changes.append(action.apply(state))

        return RunResult(state=state, trace=trace)

    @staticmethod
    def _check_conflict(
        action: Set,
        rule_id: int,
        rule: Rule,
        assignments: Dict[str, tuple],
    ) -> Optional[ConflictRecord]:
        previous = assignments.get(action.field)
        if (
            previous is None
            or previous[0] == action.value
            or previous[1] == rule_id  # same rule may overwrite its own sets
        ):
            assignments[action.field] = (action.value, rule_id, rule.name)
            return None
        return ConflictRecord(
            field=action.field,
            existing_value=previous[0],
            existing_rule=previous[2],
            attempted_value=action.value,
            attempted_rule=rule.name,
            resolution="kept earlier assignment (higher priority / earlier "
            "declaration)",
        )
