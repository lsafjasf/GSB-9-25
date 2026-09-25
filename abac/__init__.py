"""Attribute-based access control (ABAC) engine with explainable decisions."""

from .engine import Policy
from .errors import AbacError, RuleConflictError, RuleValidationError
from .model import Rule, detect_conflicts, load_rules

__version__ = "1.0.0"
__all__ = ["Policy", "Rule", "load_rules", "detect_conflicts",
           "AbacError", "RuleConflictError", "RuleValidationError"]
