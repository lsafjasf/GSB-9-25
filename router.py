"""Segment-trie HTTP router.

Features
--------
- Segment kinds: static, ``{param}``, single-segment wildcard ``*``,
  tail wildcard ``**`` (must be the last segment, matches zero or more
  remaining segments).
- Optional per-rule filtering by HTTP method and host prefix.
- Deterministic, fully-specified priority (see README):
  static > param > wildcard; ties broken by registration order.
- Match results carry extracted params (raw + percent-decoded) and the
  list of shadowed candidate rules that also matched but lost.
- Percent-decoding is strict: invalid ``%XX`` sequences or invalid
  UTF-8 raise :class:`DecodeError` pointing at the segment index.
- Load-time conflict detection: a rule whose every possible match is
  already won by an earlier rule is reported with its line number.

Only the Python standard library is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple
from urllib.parse import unquote

# Segment kinds, ordered by priority (lower wins).
STATIC, PARAM, WILD, TAILWILD = 0, 1, 2, 3

_PARAM_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class RouteError(Exception):
    """Base class for router errors."""


class PatternError(RouteError):
    """A route pattern is malformed."""


class ConflictError(RouteError):
    """A rule is fully shadowed by an earlier rule (or duplicates it)."""


class DecodeError(RouteError):
    """A path segment failed strict percent-decoding."""

    def __init__(self, segment_index: int, raw: str, reason: str):
        self.segment_index = segment_index
        self.raw = raw
        self.reason = reason
        super().__init__(
            "failed to decode segment %d (%r): %s" % (segment_index, raw, reason)
        )


@dataclass(frozen=True)
class Segment:
    kind: int
    literal: str = ""  # STATIC: the literal text
    name: str = ""     # PARAM: the parameter name


@dataclass
class Rule:
    pattern: str
    segments: Tuple[Segment, ...]
    method: Optional[str]        # None = any method
    host_prefix: Optional[str]   # None = any host
    index: int                   # registration order
    line: Optional[int] = None
    payload: Any = None

    def describe(self) -> str:
        method = self.method or "*"
        host = self.host_prefix or "*"
        where = "line %s: " % self.line if self.line is not None else ""
        return "%s%s %s %s" % (where, method, host, self.pattern)


@dataclass
class Param:
    name: str
    raw: str
    value: str


@dataclass
class Match:
    rule: Rule
    params: List[Param]
    shadowed: List[Rule]  # candidates that also matched but lost, in priority order

    @property
    def hit_order(self) -> List[Rule]:
        """All matching candidates in priority order (winner first)."""
        return [self.rule] + list(self.shadowed)


def parse_pattern(pattern: str) -> Tuple[Segment, ...]:
    if not pattern.startswith("/"):
        raise PatternError("pattern must start with '/': %r" % pattern)
    body = pattern[1:]
    if len(body) > 1 and body.endswith("/"):
        body = body[:-1]
    if not body:
        return ()
    parts = body.split("/")
    segments: List[Segment] = []
    for pos, part in enumerate(parts):
        if part == "**":
            if pos != len(parts) - 1:
                raise PatternError(
                    "'**' must be the last segment: %r" % pattern
                )
            segments.append(Segment(TAILWILD))
        elif part == "*":
            segments.append(Segment(WILD))
        elif part == "":
            raise PatternError("empty segment in pattern: %r" % pattern)
        else:
            m = _PARAM_RE.match(part)
            if m:
                segments.append(Segment(PARAM, name=m.group(1)))
            elif part.startswith("{") or part.endswith("}"):
                raise PatternError("malformed parameter segment %r in %r" % (part, pattern))
            else:
                segments.append(Segment(STATIC, literal=part))
    return tuple(segments)


def _split_path(path: str) -> List[str]:
    body = path[1:] if path.startswith("/") else path
    if len(body) > 1 and body.endswith("/"):
        body = body[:-1]
    return body.split("/") if body else []


def _norm_method(method: Optional[str]) -> Optional[str]:
    if method is None:
        return None
    method = method.strip().upper()
    return None if method in ("", "*", "-") else method


def _norm_host_prefix(host: Optional[str]) -> Optional[str]:
    if host is None:
        return None
    host = host.strip().lower()
    return None if host in ("", "*", "-") else host


def _strict_decode(raw: str, segment_index: int) -> str:
    bad = _BAD_PERCENT_RE.search(raw)
    if bad:
        raise DecodeError(
            segment_index, raw, "invalid percent-escape at char %d" % bad.start()
        )
    try:
        return unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DecodeError(segment_index, raw, "invalid UTF-8: %s" % exc) from exc


class _Node:
    __slots__ = ("static", "param", "wild", "rules", "tail")

    def __init__(self) -> None:
        self.static = {}            # literal -> _Node
        self.param: Optional[_Node] = None
        self.wild: Optional[_Node] = None
        self.rules: List[Rule] = []  # rules terminating exactly here
        self.tail: List[Rule] = []   # rules ending with '**' at this node


def _constraint_covers(older: Rule, newer: Rule) -> bool:
    """True if the older rule's method/host constraints are a superset."""
    if older.method is not None and older.method != newer.method:
        return False
    if older.host_prefix is not None:
        if newer.host_prefix is None:
            return False
        if not newer.host_prefix.startswith(older.host_prefix):
            return False
    return True


class Router:
    """A segment-trie router. Matching never scans the full rule list."""

    def __init__(self) -> None:
        self._root = _Node()
        self._rules: List[Rule] = []

    @property
    def rules(self) -> List[Rule]:
        return list(self._rules)

    # ------------------------------------------------------------------ load
    def add_rule(
        self,
        pattern: str,
        method: Optional[str] = None,
        host_prefix: Optional[str] = None,
        payload: Any = None,
        line: Optional[int] = None,
    ) -> Rule:
        segments = parse_pattern(pattern)
        rule = Rule(
            pattern=pattern,
            segments=segments,
            method=_norm_method(method),
            host_prefix=_norm_host_prefix(host_prefix),
            index=len(self._rules),
            line=line,
            payload=payload,
        )
        shadowers: List[Rule] = []
        self._find_shadowers(self._root, segments, 0, rule, shadowers)
        if shadowers:
            msg = "%s is fully shadowed by %s" % (
                rule.describe(),
                ", ".join(r.describe() for r in shadowers),
            )
            raise ConflictError(msg)
        node = self._root
        for seg in segments:
            if seg.kind == STATIC:
                node = node.static.setdefault(seg.literal, _Node())
            elif seg.kind == PARAM:
                if node.param is None:
                    node.param = _Node()
                node = node.param
            elif seg.kind == WILD:
                if node.wild is None:
                    node.wild = _Node()
                node = node.wild
            else:  # TAILWILD, always last
                node.tail.append(rule)
                self._rules.append(rule)
                return rule
        node.rules.append(rule)
        self._rules.append(rule)
        return rule

    def _find_shadowers(
        self, node: _Node, segs: Tuple[Segment, ...], i: int, rule: Rule, out: List[Rule]
    ) -> None:
        """Find already-registered rules that fully shadow ``rule``.

        An earlier rule A fully shadows B iff A's match set is a superset of
        B's and A's priority is >= B's for every path B can match.  On the
        kind ordering static<param<wild<tailwild this reduces to a trie walk:
        """
        if i == len(segs):
            for r in node.rules:  # identical kind tuple, earlier registration wins
                if _constraint_covers(r, rule):
                    out.append(r)
            return
        seg = segs[i]
        if seg.kind == STATIC:
            # Only the identical static segment covers with >= priority.
            child = node.static.get(seg.literal)
            if child is not None:
                self._find_shadowers(child, segs, i + 1, rule, out)
        elif seg.kind == PARAM:
            # Only an existing param covers a param with >= priority.
            if node.param is not None:
                self._find_shadowers(node.param, segs, i + 1, rule, out)
        elif seg.kind == WILD:
            # Existing param or wild covers a wild with >= priority.
            if node.param is not None:
                self._find_shadowers(node.param, segs, i + 1, rule, out)
            if node.wild is not None:
                self._find_shadowers(node.wild, segs, i + 1, rule, out)
        else:  # TAILWILD (last segment)
            # Only an identical '**' tail at the same node has >= priority.
            for r in node.tail:
                if _constraint_covers(r, rule):
                    out.append(r)

    # ----------------------------------------------------------------- match
    def match(
        self,
        path: str,
        method: Optional[str] = None,
        host: Optional[str] = None,
    ) -> Optional[Match]:
        segs = _split_path(path)
        candidates: List[Rule] = []
        self._collect(self._root, segs, 0, candidates)
        method_n = _norm_method(method)
        host_n = host.lower() if host else None
        filtered = [
            r
            for r in candidates
            if (r.method is None or r.method == method_n)
            and (r.host_prefix is None or (host_n is not None and host_n.startswith(r.host_prefix)))
        ]
        if not filtered:
            return None
        winner = filtered[0]
        params = [
            Param(seg.name, segs[k], _strict_decode(segs[k], k))
            for k, seg in enumerate(winner.segments)
            if seg.kind == PARAM
        ]
        return Match(rule=winner, params=params, shadowed=filtered[1:])

    def _collect(self, node: _Node, segs: List[str], i: int, out: List[Rule]) -> None:
        """DFS yielding candidate rules in priority (lexicographic kind) order.

        At each node the static child (kind 0) is explored before the param
        child (1), then the wildcard child (2); the node's own tail-wildcard
        rules (kind 3 at this position) sort after every deeper candidate.
        """
        if i == len(segs):
            out.extend(node.rules)
        else:
            raw = segs[i]
            child = node.static.get(raw)
            if child is not None:
                self._collect(child, segs, i + 1, out)
            if node.param is not None:
                self._collect(node.param, segs, i + 1, out)
            if node.wild is not None:
                self._collect(node.wild, segs, i + 1, out)
        out.extend(node.tail)


def load_rules(router: Router, text: str) -> Router:
    """Load rules from lines of ``METHOD HOST PATTERN``.

    ``*`` or ``-`` means "any". Blank lines and ``#`` comments are skipped.
    All fully-shadowed rules are collected and reported (with 1-based line
    numbers) in a single :class:`ConflictError`.
    """
    conflicts: List[str] = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            raise PatternError(
                "line %d: expected 'METHOD HOST PATTERN', got %r" % (lineno, raw_line)
            )
        method, host, pattern = parts
        try:
            router.add_rule(pattern, method=method, host_prefix=host, line=lineno)
        except ConflictError as exc:
            conflicts.append(str(exc))
    if conflicts:
        raise ConflictError("\n".join(conflicts))
    return router
