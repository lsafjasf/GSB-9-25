"""Locale-driven human-readable time span formatting.

All language-specific knowledge (unit names, plural forms, joiners,
direction templates) lives in JSON locale configs. This module contains
no per-language branches: plural selection goes through a small registry
of generic, config-referenced plural rules.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

__all__ = [
    "LocaleConfigError",
    "Locale",
    "TimeSpanFormatter",
    "load_locale",
    "PLURAL_RULES",
]


class LocaleConfigError(ValueError):
    """Raised when a locale configuration is invalid.

    The message always names the config source and the location of the
    offending entry, e.g. ``units[3].seconds``.
    """


def _plural_other_only(value):
    return "other"


def _plural_one_other(value):
    return "one" if value == 1 else "other"


# Generic plural rules. A locale config picks one by name via
# ``plural_rule``; the config must supply forms for every category the
# rule can produce.
PLURAL_RULES = {
    "other_only": (_plural_other_only, ("other",)),
    "one_other": (_plural_one_other, ("one", "other")),
}


class _Unit:
    __slots__ = ("id", "seconds", "forms")

    def __init__(self, unit_id, seconds, forms):
        self.id = unit_id
        self.seconds = seconds
        self.forms = forms


class Locale:
    """A validated locale configuration."""

    def __init__(self, config, source="<config>"):
        if not isinstance(config, dict):
            raise LocaleConfigError(
                f"{source}: root: expected a JSON object, got {type(config).__name__}"
            )
        self.source = source
        self.language = config.get("language", "<unnamed>")

        rule_name = config.get("plural_rule")
        if rule_name not in PLURAL_RULES:
            known = ", ".join(sorted(PLURAL_RULES))
            raise LocaleConfigError(
                f"{source}: plural_rule: unknown rule {rule_name!r}; known rules: {known}"
            )
        self.plural_rule_name = rule_name
        self._plural_fn, self._required_categories = PLURAL_RULES[rule_name]

        self.joiner = self._optional_str(config, "joiner", "")
        self.component_template = self._required_str(config, "component_template")
        for placeholder in ("{value}", "{unit}"):
            if placeholder not in self.component_template:
                raise LocaleConfigError(
                    f"{source}: component_template: must contain {placeholder!r}, "
                    f"got {self.component_template!r}"
                )
        self.past_template = self._required_str(config, "past")
        self.future_template = self._required_str(config, "future")
        for name, template in (("past", self.past_template), ("future", self.future_template)):
            if "{span}" not in template:
                raise LocaleConfigError(
                    f"{source}: {name}: template must contain '{{span}}', got {template!r}"
                )

        self.below_min_unit = self._validate_below_min(config.get("below_min_unit"))
        self.units = self._validate_units(config.get("units"))

    # -- validation helpers -------------------------------------------------

    def _optional_str(self, config, key, default):
        value = config.get(key, default)
        if not isinstance(value, str):
            raise LocaleConfigError(
                f"{self.source}: {key}: expected a string, got {type(value).__name__}"
            )
        return value

    def _required_str(self, config, key):
        value = config.get(key)
        if not isinstance(value, str) or not value:
            raise LocaleConfigError(
                f"{self.source}: {key}: missing or not a non-empty string"
            )
        return value

    def _validate_below_min(self, spec):
        if spec is None:
            return {"mode": "zero"}
        if not isinstance(spec, dict):
            raise LocaleConfigError(
                f"{self.source}: below_min_unit: expected an object, got {type(spec).__name__}"
            )
        mode = spec.get("mode")
        if mode not in ("zero", "just_now"):
            raise LocaleConfigError(
                f"{self.source}: below_min_unit.mode: expected 'zero' or 'just_now', got {mode!r}"
            )
        if mode == "just_now":
            text = spec.get("text")
            if not isinstance(text, str) or not text:
                raise LocaleConfigError(
                    f"{self.source}: below_min_unit.text: required when mode is 'just_now'"
                )
            return {"mode": mode, "text": text}
        return {"mode": mode}

    def _validate_units(self, units):
        location = "units"
        if not isinstance(units, list) or not units:
            raise LocaleConfigError(
                f"{self.source}: {location}: missing or empty; at least one unit is required"
            )
        validated = []
        for index, raw in enumerate(units):
            location = f"units[{index}]"
            if not isinstance(raw, dict):
                raise LocaleConfigError(
                    f"{self.source}: {location}: expected an object, got {type(raw).__name__}"
                )
            unit_id = raw.get("id")
            if not isinstance(unit_id, str) or not unit_id:
                raise LocaleConfigError(
                    f"{self.source}: {location}.id: missing or not a non-empty string"
                )
            seconds = raw.get("seconds")
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
                raise LocaleConfigError(
                    f"{self.source}: {location}.seconds: missing or not a positive number "
                    f"(unit {unit_id!r})"
                )
            forms = raw.get("forms")
            if not isinstance(forms, dict) or not forms:
                raise LocaleConfigError(
                    f"{self.source}: {location}.forms: missing or empty (unit {unit_id!r})"
                )
            for category in self._required_categories:
                if category not in forms:
                    raise LocaleConfigError(
                        f"{self.source}: {location}.forms: missing plural form {category!r} "
                        f"required by plural_rule {self.plural_rule_name!r} (unit {unit_id!r})"
                    )
                if not isinstance(forms[category], str) or not forms[category]:
                    raise LocaleConfigError(
                        f"{self.source}: {location}.forms.{category}: not a non-empty string "
                        f"(unit {unit_id!r})"
                    )
            validated.append(_Unit(unit_id, float(seconds), forms))

        for index in range(1, len(validated)):
            prev, curr = validated[index - 1], validated[index]
            if not curr.seconds < prev.seconds:
                raise LocaleConfigError(
                    f"{self.source}: units[{index}] ({curr.id!r}, {curr.seconds:g}s) is not "
                    f"smaller than units[{index - 1}] ({prev.id!r}, {prev.seconds:g}s); "
                    f"units must be ordered from largest to smallest"
                )
        return validated

    # -- formatting primitives ----------------------------------------------

    def plural_form(self, unit, value):
        return unit.forms[self._plural_fn(value)]

    def render_component(self, unit, value):
        return self.component_template.format(
            value=value, unit=self.plural_form(unit, value)
        )


def load_locale(path):
    """Load and validate a locale config from a JSON file."""
    path = Path(path)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LocaleConfigError(f"{path}: invalid JSON: {exc}") from exc
    return Locale(config, source=str(path))


class TimeSpanFormatter:
    """Formats a span in seconds as localized human-readable text.

    Sign convention: positive seconds -> future, negative -> past,
    zero / below the smallest configured unit -> the locale's
    ``below_min_unit`` rule (``zero`` or ``just_now``).
    """

    def __init__(self, locale):
        if not isinstance(locale, Locale):
            raise TypeError("locale must be a timespan.Locale")
        self.locale = locale
        self._units = locale.units
        self._min_seconds = self._units[-1].seconds

    @classmethod
    def from_locale_file(cls, path):
        return cls(load_locale(path))

    # -- decomposition --------------------------------------------------------

    def _decompose(self, total):
        """Greedy canonical decomposition; every component is below the
        next-larger unit's ratio, so no out-of-range value can appear."""
        components = []
        remainder = total
        for unit in self._units:
            value = int(remainder // unit.seconds)
            components.append(value)
            remainder -= value * unit.seconds
        return components, remainder

    def components(self, seconds, precision=2, mode="truncate"):
        """Return ``[(unit, value), ...]`` for the most significant units.

        ``mode='truncate'`` drops the remainder (never rounds up, so a
        component can never spill into the next unit). ``mode='round'``
        rounds at the last kept unit and re-decomposes, so carries
        propagate correctly (59m 59s -> 1h, 11m 29d -> 1y, ...).
        """
        self._check_args(precision, mode)
        total = abs(float(seconds))
        if total < self._min_seconds:
            return []

        values, remainder = self._decompose(total)
        significant = [i for i, v in enumerate(values) if v > 0]
        kept = significant[:precision]

        if mode == "round" and kept:
            last = kept[-1]
            quantum = self._units[last].seconds
            below = remainder + sum(
                values[i] * self._units[i].seconds
                for i in range(last + 1, len(self._units))
            )
            if below * 2 >= quantum:
                rounded_total = sum(
                    values[i] * self._units[i].seconds for i in kept
                ) + quantum
                values, _ = self._decompose(rounded_total)
                significant = [i for i, v in enumerate(values) if v > 0]
                kept = significant[:precision]

        return [(self._units[i], values[i]) for i in kept]

    # -- public API -----------------------------------------------------------

    def format(self, seconds, precision=2, mode="truncate"):
        """Format ``seconds`` (float; sign gives direction) as text."""
        self._check_args(precision, mode)
        if not math.isfinite(float(seconds)):
            raise ValueError(f"seconds must be finite, got {seconds!r}")
        total = abs(float(seconds))

        if total < self._min_seconds:
            rule = self.locale.below_min_unit
            if rule["mode"] == "just_now":
                return rule["text"]
            smallest = self._units[-1]
            return self.locale.render_component(smallest, 0)

        parts = [
            self.locale.render_component(unit, value)
            for unit, value in self.components(total, precision, mode)
        ]
        span = self.locale.joiner.join(parts)
        if seconds < 0:
            return self.locale.past_template.format(span=span)
        return self.locale.future_template.format(span=span)

    def _check_args(self, precision, mode):
        if not isinstance(precision, int) or precision < 1:
            raise ValueError(f"precision must be an integer >= 1, got {precision!r}")
        if mode not in ("truncate", "round"):
            raise ValueError(f"mode must be 'truncate' or 'round', got {mode!r}")
