"""MessageFormatter: template rendering with plural rules, localized
numbers/dates, placeholder diagnostics and locale fallback chains."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import parser
from .errors import (ConfigError, MessageNotFoundError, RenderError,
                     TemplateSyntaxError)
from .localize import format_date, format_number
from .plural import compile_expression, select_plural


@dataclass
class Diagnostic:
    """A problem detected while checking a template against arguments."""
    code: str            # "missing-arg" | "extra-arg" | "duplicate-arg"
    name: Optional[str]  # placeholder name (None for parse-level issues)
    pos: Optional[int]   # 0-based offset in the template (None if N/A)
    message: str

    def __str__(self):
        where = f" at position {self.pos}" if self.pos is not None else ""
        return f"[{self.code}] {self.message}{where}"


@dataclass
class RenderResult:
    text: str
    locale_requested: str
    locale_used: str
    diagnostics: List[Diagnostic] = field(default_factory=list)
    plural_trace: List[dict] = field(default_factory=list)

    @property
    def fell_back(self) -> bool:
        return self.locale_used != self.locale_requested

    def explain(self) -> str:
        lines = [f"locale: requested={self.locale_requested} "
                 f"used={self.locale_used}"]
        for entry in self.plural_trace:
            lines.append(f"plural({entry['arg']}={entry['value']!r}) "
                         f"-> {entry['category']}")
            for step in entry["evaluations"]:
                when = step["when"] if step["when"] is not None else "<default>"
                lines.append(f"    {step['category']}: {when} -> {step['result']}")
        for diag in self.diagnostics:
            lines.append(str(diag))
        return "\n".join(lines)


def _validate_number_cfg(locale, cfg):
    if not isinstance(cfg, dict):
        raise ConfigError(f"locale {locale!r}: 'number' must be an object")
    for key in ("thousands_sep", "decimal_sep"):
        if not isinstance(cfg.get(key), str):
            raise ConfigError(f"locale {locale!r}: number.{key} must be a string")
    places = cfg.get("decimal_places")
    if places is not None and (not isinstance(places, int) or places < 0):
        raise ConfigError(f"locale {locale!r}: number.decimal_places must be "
                          f"null or a non-negative integer")


def _validate_date_cfg(locale, cfg):
    if not isinstance(cfg, dict):
        raise ConfigError(f"locale {locale!r}: 'date' must be an object")
    order = cfg.get("order")
    if not isinstance(order, str) or sorted(order) != ["D", "M", "Y"]:
        raise ConfigError(f"locale {locale!r}: date.order must be a "
                          f"permutation of 'YMD', got {order!r}")
    if not isinstance(cfg.get("separator"), str):
        raise ConfigError(f"locale {locale!r}: date.separator must be a string")


def _compile_plural_cfg(locale, cfg):
    if not isinstance(cfg, dict) or not isinstance(cfg.get("categories"), list):
        raise ConfigError(f"locale {locale!r}: 'plural.categories' must be a list")
    categories = cfg["categories"]
    if not categories:
        raise ConfigError(f"locale {locale!r}: plural.categories is empty")
    compiled = []
    seen = set()
    catch_all = 0
    for entry in categories:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ConfigError(f"locale {locale!r}: each plural category needs "
                              f"a string 'name'")
        name = entry["name"]
        if name in seen:
            raise ConfigError(f"locale {locale!r}: duplicate plural category "
                              f"{name!r}")
        seen.add(name)
        when = entry.get("when")
        if when is None:
            catch_all += 1
            if name != "other":
                raise ConfigError(f"locale {locale!r}: the catch-all plural "
                                  f"category (when=null) must be named "
                                  f"'other', got {name!r}")
            compiled.append((name, None, None))
        else:
            if not isinstance(when, str):
                raise ConfigError(f"locale {locale!r}: plural rule for "
                                  f"{name!r} must be a string or null")
            compiled.append((name, compile_expression(when), when))
    if catch_all != 1:
        raise ConfigError(f"locale {locale!r}: exactly one plural category "
                          f"must have when=null (found {catch_all})")
    return compiled


class MessageFormatter:
    def __init__(self, config: dict, catalogs: Dict[str, Dict[str, str]]):
        if not isinstance(config, dict) or not isinstance(
                config.get("locales"), dict) or not config["locales"]:
            raise ConfigError("config must contain a non-empty 'locales' object")
        self.locales = config["locales"]
        self.catalogs = catalogs
        self._plural = {}
        for name, lc in self.locales.items():
            if not isinstance(lc, dict):
                raise ConfigError(f"locale {name!r}: entry must be an object")
            _validate_number_cfg(name, lc.get("number"))
            _validate_date_cfg(name, lc.get("date"))
            self._plural[name] = _compile_plural_cfg(name, lc.get("plural"))
            fallback = lc.get("fallback", [])
            if not isinstance(fallback, list) or not all(
                    isinstance(x, str) for x in fallback):
                raise ConfigError(f"locale {name!r}: 'fallback' must be a "
                                  f"list of locale names")
        for name, lc in self.locales.items():
            for target in lc.get("fallback", []):
                if target not in self.locales:
                    raise ConfigError(f"locale {name!r}: fallback target "
                                      f"{target!r} is not a declared locale")
        self._template_cache: Dict[tuple, list] = {}

    @classmethod
    def from_dir(cls, config_path: str, messages_dir: str) -> "MessageFormatter":
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
        catalogs = {}
        for fname in sorted(os.listdir(messages_dir)):
            if fname.endswith(".json"):
                with open(os.path.join(messages_dir, fname),
                          encoding="utf-8") as fh:
                    catalogs[fname[:-5]] = json.load(fh)
        return cls(config, catalogs)

    # -- lookup ---------------------------------------------------------

    def fallback_chain(self, locale: str) -> List[str]:
        if locale not in self.locales:
            raise ConfigError(f"unknown locale {locale!r}")
        return [locale] + list(self.locales[locale].get("fallback", []))

    def _resolve(self, locale: str, key: str):
        for candidate in self.fallback_chain(locale):
            catalog = self.catalogs.get(candidate)
            if catalog and key in catalog:
                return candidate, catalog[key]
        raise MessageNotFoundError(key, locale)

    def _nodes(self, locale: str, key: str, template: str) -> list:
        cache_key = (locale, key)
        nodes = self._template_cache.get(cache_key)
        if nodes is None:
            nodes = parser.parse(template)
            self._template_cache[cache_key] = nodes
        return nodes

    # -- diagnostics ----------------------------------------------------

    @staticmethod
    def _arg_usages(nodes, usages=None):
        if usages is None:
            usages = {}
        for node in nodes:
            if isinstance(node, (parser.Arg, parser.Plural)):
                usages.setdefault(node.name, []).append(node.pos)
            if isinstance(node, parser.Plural):
                for body in node.options.values():
                    MessageFormatter._arg_usages(body, usages)
        return usages

    def _diagnostics(self, nodes, args) -> List[Diagnostic]:
        usages = self._arg_usages(nodes)
        diags = []
        for name, positions in usages.items():
            if name not in args:
                diags.append(Diagnostic(
                    "missing-arg", name, positions[0],
                    f"placeholder {name!r} is used but no argument was given"))
            for pos in positions[1:]:
                diags.append(Diagnostic(
                    "duplicate-arg", name, pos,
                    f"placeholder {name!r} is used more than once"))
        for name in args:
            if name not in usages:
                diags.append(Diagnostic(
                    "extra-arg", name, None,
                    f"argument {name!r} was given but is never used"))
        return diags

    def validate(self, locale: str, key: str, args: Optional[dict] = None
                 ) -> List[Diagnostic]:
        """Check a message template (and optionally its arguments) without
        rendering. Returns a list of Diagnostics; raises TemplateSyntaxError
        for malformed templates."""
        used_locale, template = self._resolve(locale, key)
        nodes = self._nodes(used_locale, key, template)
        return self._diagnostics(nodes, args or {})

    # -- rendering ------------------------------------------------------

    def render(self, locale: str, key: str, args: Optional[dict] = None,
               strict: bool = False) -> RenderResult:
        args = dict(args or {})
        used_locale, template = self._resolve(locale, key)
        nodes = self._nodes(used_locale, key, template)
        diagnostics = self._diagnostics(nodes, args)
        if strict and diagnostics:
            raise RenderError("strict render failed: " + "; ".join(
                str(d) for d in diagnostics))
        trace: List[dict] = []
        text = self._render_nodes(nodes, args, used_locale, trace, None)
        return RenderResult(text, locale, used_locale, diagnostics, trace)

    def _format_arg(self, node, value, locale):
        cfg = self.locales[locale]
        if node.kind == "number":
            return format_number(value, cfg["number"])
        if node.kind == "date":
            return format_date(value, cfg["date"])
        return str(value)

    def _render_nodes(self, nodes, args, locale, trace, hash_value) -> str:
        out = []
        for node in nodes:
            if isinstance(node, parser.Text):
                out.append(node.value)
            elif isinstance(node, parser.Hash):
                out.append(hash_value if hash_value is not None else "#")
            elif isinstance(node, parser.Arg):
                if node.name in args:
                    out.append(self._format_arg(node, args[node.name], locale))
                else:
                    out.append("{" + node.name + "}")
            elif isinstance(node, parser.Plural):
                if node.name not in args:
                    out.append("{" + node.name + "}")
                    continue
                value = args[node.name]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise RenderError(
                        f"plural argument {node.name!r} must be a number, "
                        f"got {type(value).__name__}")
                category, evaluations = select_plural(
                    self._plural[locale], value)
                trace.append({"arg": node.name, "value": value,
                              "category": category,
                              "evaluations": evaluations})
                body = node.options.get(category)
                if body is None:
                    body = node.options.get("other")
                if body is None:
                    raise RenderError(
                        f"template has no option for plural category "
                        f"{category!r} and no 'other'")
                formatted = format_number(value, self.locales[locale]["number"])
                out.append(self._render_nodes(body, args, locale, trace,
                                              formatted))
        return "".join(out)
