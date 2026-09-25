"""Exception types for the ABAC engine."""


class AbacError(Exception):
    """Base class for all ABAC errors."""


class RuleValidationError(AbacError):
    """A rule is malformed (bad effect, bad operator, duplicate id, ...)."""


class RuleConflictError(AbacError):
    """Two or more rules share identical conditions but opposite effects."""

    def __init__(self, conflicts):
        self.conflicts = conflicts  # list of {"conditions": str, "rule_ids": [...]}
        lines = ["conflicting rules detected (same conditions, opposite effects):"]
        for c in conflicts:
            lines.append("  rules %s share conditions %s" % (c["rule_ids"], c["conditions"]))
        super().__init__("\n".join(lines))
