"""Locale-aware number and date formatting, driven by language config.

No language-specific format is hardcoded here: thousands/decimal
separators, decimal places, date field order and padding all come from
the locale's config entry.
"""
from __future__ import annotations

import datetime

from .errors import RenderError

_SENTINEL = "\x00"


def format_number(value, cfg: dict) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RenderError(f"expected a number, got {type(value).__name__}")
    places = cfg.get("decimal_places")
    if places is not None:
        text = f"{value:,.{places}f}"
    elif isinstance(value, int):
        text = f"{value:,}"
    elif value.is_integer():
        text = f"{int(value):,}"
    else:
        text = f"{value:,.10f}".rstrip("0").rstrip(".")
    thousands = cfg["thousands_sep"]
    decimal = cfg["decimal_sep"]
    return (text.replace(",", _SENTINEL)
                .replace(".", decimal)
                .replace(_SENTINEL, thousands))


def format_date(value, cfg: dict) -> str:
    if isinstance(value, str):
        try:
            value = datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise RenderError(f"cannot parse ISO date {value!r}") from exc
    elif isinstance(value, datetime.datetime):
        value = value.date()
    elif not isinstance(value, datetime.date):
        raise RenderError(f"expected a date, got {type(value).__name__}")
    pad = cfg.get("pad", True)
    parts = {
        "Y": f"{value.year:04d}",
        "M": f"{value.month:02d}" if pad else str(value.month),
        "D": f"{value.day:02d}" if pad else str(value.day),
    }
    return cfg["separator"].join(parts[ch] for ch in cfg["order"])
