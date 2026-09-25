"""Recursive-descent parser for message templates.

Template grammar:
    template := (text | placeholder | plural)*
    placeholder := "{" name ("," kind)? "}"        kind: string|number|date
    plural := "{" name ", plural," (category "{" template "}")+ "}"
    Inside a plural option, "#" expands to the formatted quantity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .errors import TemplateSyntaxError

_ARG_KINDS = {"string", "number", "date"}


@dataclass
class Text:
    value: str


@dataclass
class Hash:
    pos: int


@dataclass
class Arg:
    name: str
    kind: str  # "string" | "number" | "date"
    pos: int


@dataclass
class Plural:
    name: str
    options: Dict[str, list]
    pos: int


def parse(template: str) -> list:
    """Parse a template into a list of nodes. Raises TemplateSyntaxError."""
    nodes, _ = _parse_nodes(template, 0, in_plural=False, stop_at="")
    return nodes


def _skip_ws(t: str, i: int) -> int:
    while i < len(t) and t[i] in " \t\n":
        i += 1
    return i


def _read_ident(t: str, i: int):
    if i >= len(t) or not (t[i].isalpha() or t[i] == "_"):
        raise TemplateSyntaxError("expected an identifier", i)
    j = i
    while j < len(t) and (t[j].isalnum() or t[j] in "_-."):
        j += 1
    return t[i:j], j


def _parse_nodes(t: str, i: int, in_plural: bool, stop_at: str):
    nodes: List = []
    buf: List[str] = []

    def flush():
        if buf:
            nodes.append(Text("".join(buf)))
            buf.clear()

    while i < len(t):
        c = t[i]
        if stop_at and c == stop_at:
            break
        if c == "{":
            flush()
            node, i = _parse_tag(t, i)
            nodes.append(node)
        elif c == "#" and in_plural:
            flush()
            nodes.append(Hash(i))
            i += 1
        elif c == "}":
            raise TemplateSyntaxError("unmatched '}'", i)
        else:
            buf.append(c)
            i += 1
    flush()
    return nodes, i


def _parse_tag(t: str, i: int):
    start = i
    i = _skip_ws(t, i + 1)  # consume '{'
    name, i = _read_ident(t, i)
    i = _skip_ws(t, i)
    if i < len(t) and t[i] == "}":
        return Arg(name, "string", start), i + 1
    if i >= len(t) or t[i] != ",":
        raise TemplateSyntaxError("expected ',' or '}'", i)
    i = _skip_ws(t, i + 1)
    kind, i = _read_ident(t, i)
    i = _skip_ws(t, i)
    if kind in _ARG_KINDS:
        if i >= len(t) or t[i] != "}":
            raise TemplateSyntaxError("expected '}'", i)
        return Arg(name, kind, start), i + 1
    if kind != "plural":
        raise TemplateSyntaxError(f"unknown placeholder type {kind!r}", i)
    if i >= len(t) or t[i] != ",":
        raise TemplateSyntaxError("expected ',' after 'plural'", i)
    i += 1
    options: Dict[str, list] = {}
    while True:
        i = _skip_ws(t, i)
        if i >= len(t):
            raise TemplateSyntaxError("unterminated plural block", start)
        if t[i] == "}":
            if not options:
                raise TemplateSyntaxError("plural needs at least one option", start)
            return Plural(name, options, start), i + 1
        category, i = _read_ident(t, i)
        i = _skip_ws(t, i)
        if i >= len(t) or t[i] != "{":
            raise TemplateSyntaxError("expected '{' after plural category", i)
        body, i = _parse_nodes(t, i + 1, in_plural=True, stop_at="}")
        if i >= len(t):
            raise TemplateSyntaxError("unterminated plural option", start)
        i += 1  # consume '}'
        if category in options:
            raise TemplateSyntaxError(
                f"duplicate plural category {category!r}", start)
        options[category] = body
