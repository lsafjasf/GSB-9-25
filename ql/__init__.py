"""Tiny query DSL parser package."""
from .errors import ParseError, ErrorCode
from .api import parse_query

__all__ = ["parse_query", "ParseError", "ErrorCode"]
