"""Segment-based HTTP router: static / param / wildcard segments, method & host filters.

Pattern syntax (path is split on '/', empty segments are ignored):
    users          static segment, matches the literal text
    {id}           parameter segment, matches any single non-empty segment
    *              single-segment wildcard, matches any single non-empty segment
    **             tail wildcard, matches zero or more remaining segments (must be last)

Priority (total order, see README):
    static > param > `*` > `**`, compared segment-by-segment from the left;
    the first differing segment decides. Ties are broken by registration order
    (earlier registration wins). Method / host are filters, not priority keys.

Matching uses a segment trie, so a lookup touches O(path depth) nodes instead of
scanning every rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote_to_bytes

# Segment kinds, ordered by priority rank (higher wins).
STATIC = 3
PARAM = 2
STAR = 1
STARSTAR = 0

_KIND_NAMES = {STATIC: "static", PARAM: "param", STAR: "*", STARSTAR: "**"}

_PARAM_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_PCT_RE = re.compile(r"%[0-9A-Fa-f]{2}")


# --------------------------------------------------------------------------- errors

class RouteError(Exception):
    """Base class for all router errors."""


class PatternError(RouteError):
    """A route pattern is malformed."""

    def __init__(self, message: str, lineno: Optional[int] = None):
        self.lineno = lineno
        prefix = f"line {lineno}: " if lineno is not None else ""
        super().__init__(prefix + message)


class ParamDecodeError(RouteError):
    """A path segment captured by a parameter is not valid percent-encoding/UTF-8.

    `index` is the 0-based position of the offending segment within the
    request path (empty segments ignored)."""

    def __init__(self, index: int, raw: str, reason: str):
        self.index = index
        self.raw = raw
        self.reason = reason
        super().__init__(
            f"cannot decode path segment #{index} ({raw!r}): {reason}"
        )


class RouteConflictError(RouteError):
    """A newly added rule is a duplicate of, or fully shadowed by, an earlier rule."""

    def __init__(self, issues: list["Conflict"]):
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues))


class RouteLoadError(RouteError):
    """Aggregated problems found while loading a route table."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


# --------------------------------------------------------------------------- model

@dataclass(frozen=True)
class Segment:
    kind: int
    text: str  # static text, or param name, or '*' / '**'

    def __str__(self) -> str:
        if self.kind == STATIC:
            return self.text
        if self.kind == PARAM:
            return "{" + self.text + "}"
        return self.text


@dataclass(frozen=True)
class Rule:
    method: Optional[str]       # uppercase method, None = any
    host_prefix: str            # lowercase host prefix, '' = any
    pattern: str                # original path pattern
    segments: tuple             # tuple[Segment, ...]
    order: int                  # registration index
    lineno: Optional[int] = None

    def __str__(self) -> str:
        loc = f" (line {self.lineno})" if self.lineno is not None else ""
        return f"{self.method or '*'} {self.host_prefix or '*'} {self.pattern}{loc}"


@dataclass(frozen=True)
class Param:
    name: str
    raw: str      # segment as it appeared in the request path
    value: str    # percent-decoded, UTF-8 validated value


@dataclass(frozen=True)
class Conflict:
    kind: str          # 'duplicate' | 'shadowed'
    earlier: Rule
    later: Rule

    def __str__(self) -> str:
        if self.kind == "duplicate":
            what = "duplicate of"
        else:
            what = "fully shadowed by"
        return f"{self.later} is {what} {self.earlier}"


@dataclass
class MatchResult:
    rule: Rule
    params: dict                # name -> Param (raw + decoded)
    candidates: list            # every rule that matched, in priority (hit) order
    shadowed: list              # candidates that lost to the winner

    def explain(self) -> str:
        lines = [f"hit: {self.rule}"]
        for i, r in enumerate(self.shadowed, 1):
            lines.append(f"  shadowed[{i}]: {r}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- parsing

def parse_pattern(path: str, lineno: Optional[int] = None) -> tuple:
    if not path.startswith("/"):
        raise PatternError(f"pattern must start with '/': {path!r}", lineno)
    segments = []
    for raw in path.split("/"):
        if raw == "":
            continue
        if raw == "**":
            segments.append(Segment(STARSTAR, "**"))
            continue
        if segments and segments[-1].kind == STARSTAR:
            raise PatternError(
                f"'**' must be the last segment: {path!r}", lineno
            )
        if raw == "*":
            segments.append(Segment(STAR, "*"))
        elif raw.startswith("{") or raw.endswith("}"):
            m = _PARAM_RE.match(raw)
            if not m:
                raise PatternError(
                    f"malformed parameter segment {raw!r} in {path!r}", lineno
                )
            segments.append(Segment(PARAM, m.group(1)))
        else:
            segments.append(Segment(STATIC, raw))
    return tuple(segments)


def decode_param(raw: str, index: int) -> str:
    """Percent-decode one captured segment; reject malformed encodings."""
    i = raw.find("%")
    while i != -1:
        if not _PCT_RE.match(raw, i):
            raise ParamDecodeError(
                index, raw, f"invalid percent-encoding at char {i}"
            )
        i = raw.find("%", i + 3)
    try:
        data = unquote_to_bytes(raw)
    except Exception as exc:  # pragma: no cover - defensive
        raise ParamDecodeError(index, raw, str(exc)) from exc
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ParamDecodeError(index, raw, f"invalid UTF-8 ({exc})") from exc


# --------------------------------------------------------------------------- trie

class _Node:
    __slots__ = ("static_children", "param_child",
                 "star_child", "starstar_rules", "rules")

    def __init__(self) -> None:
        self.static_children: dict = {}
        self.param_child: Optional[_Node] = None
        self.star_child: Optional[_Node] = None
        self.starstar_rules: list = []   # rules ending in '**' at this node
        self.rules: list = []            # rules ending exactly at this node


def _matches_scope(rule: Rule, method: Optional[str], host: str) -> bool:
    if rule.method is not None and rule.method != method:
        return False
    return host.startswith(rule.host_prefix)


# --------------------------------------------------------------------------- router

class Router:
    def __init__(self) -> None:
        self._root = _Node()
        self.rules: list = []
        self._counter = 0

    # -- registration ------------------------------------------------------

    def add_route(self, method: Optional[str], host_prefix: str, path: str,
                  lineno: Optional[int] = None) -> Rule:
        segments = parse_pattern(path, lineno)
        rule = Rule(
            method=method.upper() if method and method not in ("-", "*") else None,
            host_prefix="" if host_prefix in ("", "-", "*") else host_prefix.lower(),
            pattern=path,
            segments=segments,
            order=self._counter,
            lineno=lineno,
        )
        self._counter += 1
        conflicts = self.check_conflicts(rule)
        if conflicts:
            raise RouteConflictError(conflicts)
        self._insert(rule)
        self.rules.append(rule)
        return rule

    def _insert(self, rule: Rule) -> None:
        node = self._root
        for seg in rule.segments:
            if seg.kind == STATIC:
                node = node.static_children.setdefault(seg.text, _Node())
            elif seg.kind == PARAM:
                if node.param_child is None:
                    node.param_child = _Node()
                node = node.param_child
            elif seg.kind == STAR:
                if node.star_child is None:
                    node.star_child = _Node()
                node = node.star_child
            else:  # STARSTAR
                node.starstar_rules.append(rule)
                return
        node.rules.append(rule)

    # -- conflict detection ------------------------------------------------

    def check_conflicts(self, new: Rule) -> list:
        """Compare `new` against already registered rules (not yet inserted)."""
        issues = []
        for old in self.rules:
            if (old.method == new.method
                    and old.host_prefix == new.host_prefix
                    and old.pattern == new.pattern):
                issues.append(Conflict("duplicate", old, new))
            elif subsumes(old, new):
                issues.append(Conflict("shadowed", old, new))
        return issues

    # -- matching ----------------------------------------------------------

    def match(self, method: Optional[str], host: str, path: str
              ) -> Optional[MatchResult]:
        method = method.upper() if method else None
        host = host.lower()
        path = path.split("?", 1)[0]
        segs = [s for s in path.split("/") if s != ""]
        decoded: dict = {}   # request-segment index -> decoded value (lazy cache)
        found: list = []     # rules in priority order
        self._walk(self._root, segs, 0, decoded, method, host, found)
        if not found:
            return None
        candidates = found
        return MatchResult(
            rule=candidates[0],
            params=_extract_params(candidates[0], segs, decoded),
            candidates=candidates,
            shadowed=candidates[1:],
        )

    def _walk(self, node: _Node, segs: list, i: int, decoded: dict,
              method: Optional[str], host: str, found: list) -> None:
        # Rules are collected in DFS order static -> param -> '*' -> '**',
        # which is exactly the priority order (see module docstring).
        if i == len(segs):
            for rule in node.rules:
                if _matches_scope(rule, method, host):
                    found.append(rule)
            for rule in node.starstar_rules:
                if _matches_scope(rule, method, host):
                    found.append(rule)
            return
        seg = segs[i]
        child = node.static_children.get(seg)
        if child is not None:
            self._walk(child, segs, i + 1, decoded, method, host, found)
        if node.param_child is not None:
            # Validate the encoding as soon as a param edge is taken; a
            # malformed segment must never pass through as a plain string.
            if i not in decoded:
                decoded[i] = decode_param(seg, i)
            self._walk(node.param_child, segs, i + 1, decoded, method, host, found)
        if node.star_child is not None:
            self._walk(node.star_child, segs, i + 1, decoded, method, host, found)
        for rule in node.starstar_rules:
            if _matches_scope(rule, method, host):
                found.append(rule)


def _extract_params(rule: Rule, segs: list, decoded: dict) -> dict:
    """Rebuild the param mapping for a matched rule from its own segments.

    Names come from the rule itself, so sibling rules sharing a trie param
    edge (e.g. /a/{x} and /a/{y}/b) each see their own names.
    """
    params = {}
    for idx, seg in enumerate(rule.segments):
        if seg.kind == PARAM:
            raw = segs[idx]
            params[seg.text] = Param(seg.text, raw, decoded[idx])
        elif seg.kind == STARSTAR:
            break
    return params


# --------------------------------------------------------------------------- subsumption

def _seg_covers(a: Segment, b: Segment) -> bool:
    """True if segment a matches every path segment that b matches."""
    if a.kind == STATIC:
        return b.kind == STATIC and a.text == b.text
    if a.kind in (PARAM, STAR):
        # param and '*' both match any single segment, hence also any static
        return b.kind in (STATIC, PARAM, STAR)
    return True  # STARSTAR, handled positionally by the caller


def _priority_ge(a: Rule, b: Rule) -> bool:
    """True if rule a wins (or ties) against b on any path both can match.

    Compares segment-kind ranks left to right; the first differing segment
    decides. If one kind sequence is a prefix of the other (possible only
    when the longer one ends in '**'), the shorter, more exact rule wins.
    """
    ra = [s.kind for s in a.segments]
    rb = [s.kind for s in b.segments]
    for ka, kb in zip(ra, rb):
        if ka != kb:
            return ka > kb
    return len(ra) <= len(rb)


def subsumes(a: Rule, b: Rule) -> bool:
    """True if rule b can never win because rule a matches a superset of
    b's requests *and* has greater-or-equal priority on every shared path.

    This is a conservative pairwise check: it catches exact duplicates and
    rules fully covered by a single earlier rule.  (A rule covered only by
    the union of several earlier rules is not reported.)
    """
    if a.method is not None and a.method != b.method:
        return False
    if not b.host_prefix.startswith(a.host_prefix):
        return False
    sa, sb = a.segments, b.segments
    a_tail = bool(sa) and sa[-1].kind == STARSTAR
    b_tail = bool(sb) and sb[-1].kind == STARSTAR
    if a_tail:
        head = sa[:-1]
        if len(sb) < len(head):
            return False
        if not all(_seg_covers(x, y) for x, y in zip(head, sb)):
            return False
    else:
        if b_tail or len(sa) != len(sb):
            return False
        if not all(_seg_covers(x, y) for x, y in zip(sa, sb)):
            return False
    return _priority_ge(a, b)


# --------------------------------------------------------------------------- loading

def load_routes(text: str, strict: bool = True) -> Router:
    """Load rules from text. One rule per line:

        METHOD  HOST_PREFIX  PATH
        METHOD  PATH                 (host filter omitted)

    '-' means "any". Blank lines and lines starting with '#' are ignored.
    With strict=True (default) any malformed pattern, duplicate or fully
    shadowed rule aborts the load with a RouteLoadError naming the lines.
    """
    router = Router()
    problems: list[str] = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2:
            method, host, path = parts[0], "-", parts[1]
        elif len(parts) == 3:
            method, host, path = parts
        else:
            problems.append(f"line {lineno}: expected 2 or 3 fields, got {len(parts)}")
            continue
        try:
            router.add_route(method, host, path, lineno=lineno)
        except RouteConflictError as exc:
            problems.extend(str(i) for i in exc.issues)
        except PatternError as exc:
            problems.append(str(exc))
    if strict and problems:
        raise RouteLoadError(problems)
    router.load_problems = problems
    return router
