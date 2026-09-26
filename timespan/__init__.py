"""Locale-driven human-readable time span formatting (stdlib only)."""

from .core import (
    Locale,
    LocaleConfigError,
    TimeSpanFormatter,
    load_locale,
)

__version__ = "1.0.0"
__all__ = ["Locale", "LocaleConfigError", "TimeSpanFormatter", "load_locale"]
