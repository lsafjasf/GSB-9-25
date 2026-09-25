"""Explainable attribute-based access control (ABAC). Standard library only."""

from .conditions import AttributeMissingError, ConditionTypeError
from .engine import Decision, evaluate
from .ruleset import Rule, RuleSet, RuleSetError

__all__ = [
    "AttributeMissingError",
    "ConditionTypeError",
    "Decision",
    "Rule",
    "RuleSet",
    "RuleSetError",
    "evaluate",
]
__version__ = "1.0.0"
