"""Range expressions: parsing and matching.

Grammar (whitespace separates comparators inside a set, ``||`` separates
alternative sets; a version must satisfy *every* comparator of *at least
one* set)::

    range      := set ('||' set)*            | '' (empty == match any)
    set        := comparator+
    comparator := op? partial
    op         := '<' | '<=' | '>' | '>=' | '=' | '==' | '!=' | '!' | '~' | '^'
    partial    := ('x'|'X'|'*'|N)('.' ('x'|'X'|'*'|N))('.' ('x'|'X'|'*'|N)
                  ('-' prerelease)? ('+' build)?)?)?
                  (prerelease/build only allowed on a full X.Y.Z version)

Desugaring (``[a, b)`` style bounds over SemVer precedence):

    =1.2.3 / 1.2.3   -> == 1.2.3            !=1.2.3   -> /= 1.2.3
    1.2.x / 1.2      -> >=1.2.0 <1.3.0      !=1.2.x   -> NOT (>=1.2.0 <1.3.0)
    1.x / 1          -> >=1.0.0 <2.0.0      * / x     -> any version
    >1.2.x           -> >=1.3.0             <1.2.x    -> <1.2.0
    >=1.2.x          -> >=1.2.0             <=1.2.x   -> <1.3.0
    ~1.2.3           -> >=1.2.3 <1.3.0      ~1.2      -> >=1.2.0 <1.3.0
    ~1               -> >=1.0.0 <2.0.0
    ^1.2.3           -> >=1.2.3 <2.0.0      ^0.2.3    -> >=0.2.3 <0.3.0
    ^0.0.3           -> >=0.0.3 <0.0.4      ^0.0      -> >=0.0.0 <0.1.0
    ^0               -> >=0.0.0 <1.0.0

Boundary behaviour:

* ``>=`` / ``<=`` include the boundary version, ``>`` / ``<`` exclude it.
* An empty interval (e.g. ``>2.0.0 <2.0.0``) matches nothing.
* ``>*``, ``<*``, ``!=*`` match nothing; ``*`` with any other operator (or
  none) matches everything.
* Build metadata in a range version is ignored (as in comparisons).

Prerelease rule: by default a *version* carrying a prerelease tag never
matches; pass ``include_prerelease=True`` to let prerelease versions take
part in normal comparison.  (Deliberately simpler than node-semver, which
additionally special-cases comparators mentioning the same X.Y.Z.)
"""

import re

from .errors import RangeParseError
from .version import Version, _DIGITS, _parse_ident_list

_OP_PREFIX = re.compile(r"(<=|>=|==|!=|<|>|=|~|\^|!)")
_WILDCARDS = ("x", "X", "*")
_RELEASE = (1,)

_OP_ALIASES = {"==": "=", "!": "!="}


def _tokenize(text):
    """Split a range string into (token, position) pairs."""
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text.startswith("||", i):
            tokens.append(("||", i))
            i += 2
            continue
        if text[i] == "|":
            raise RangeParseError("expected '||'", i, text)
        j = i
        while j < n and not text[j].isspace() and text[j] != "|":
            j += 1
        tokens.append((text[i:j], i))
        i = j
    return tokens


class _Partial:
    """A possibly-wildcard version: nums has length 3, None = wildcard."""

    __slots__ = ("nums", "prerelease", "build")

    def __init__(self, nums, prerelease=(), build=()):
        self.nums = nums
        self.prerelease = prerelease
        self.build = build

    @property
    def precision(self):
        count = 0
        for n in self.nums:
            if n is None:
                break
            count += 1
        return count


def _parse_partial(body, offset, source):
    build = ()
    prerelease = ()
    core = body
    plus = body.find("+")
    if plus != -1:
        build, _ = _parse_ident_list(
            body[plus + 1 :], 0, source, RangeParseError, "build",
            strict_numeric=False, offset=offset + plus + 1,
        )
        core = body[:plus]
    dash = core.find("-")
    if dash != -1:
        prerelease, _ = _parse_ident_list(
            core[dash + 1 :], 0, source, RangeParseError, "prerelease",
            strict_numeric=True, offset=offset + dash + 1,
        )
        core = core[:dash]
    parts = core.split(".")
    if len(parts) > 3:
        pos = offset + sum(len(p) + 1 for p in parts[:3])
        raise RangeParseError("expected at most three version parts", pos, source)
    nums = []
    seen_wildcard = False
    cursor = offset
    for part in parts:
        if part in _WILDCARDS:
            nums.append(None)
            seen_wildcard = True
        elif part and all(c in _DIGITS for c in part):
            if seen_wildcard:
                raise RangeParseError(
                    "number cannot follow a wildcard", cursor, source
                )
            if len(part) > 1 and part[0] == "0":
                raise RangeParseError("leading zero in version number", cursor, source)
            nums.append(int(part))
        elif part == "":
            raise RangeParseError("empty version part", cursor, source)
        else:
            raise RangeParseError(
                "invalid version part %r" % part, cursor, source
            )
        cursor += len(part) + 1
    while len(nums) < 3:
        nums.append(None)
    if (prerelease or build) and any(n is None for n in nums):
        bad = offset + (dash if prerelease else plus)
        raise RangeParseError(
            "wildcard or partial version cannot carry prerelease/build metadata",
            bad,
            source,
        )
    return _Partial(nums, prerelease, build)


def _bump_key(concrete, precision):
    """Upper-bound precedence key for a wildcard range: increment the last
    concrete component, zero the rest (e.g. 1.2.x -> key of 1.3.0)."""
    bumped = concrete[:precision]
    bumped[-1] += 1
    bumped += [0] * (3 - precision)
    return (bumped[0], bumped[1], bumped[2], _RELEASE)


def _caret_upper_key(concrete, precision):
    if concrete[0] != 0:
        return (concrete[0] + 1, 0, 0, _RELEASE)
    if precision == 1:
        return (1, 0, 0, _RELEASE)
    if concrete[1] != 0:
        return (0, concrete[1] + 1, 0, _RELEASE)
    if precision == 2:
        return (0, 1, 0, _RELEASE)
    return (0, 0, concrete[2] + 1, _RELEASE)


class Comparator:
    """A single comparator such as ``>=1.2.0`` or ``~1.2``."""

    __slots__ = ("op", "partial")

    def __init__(self, op, partial):
        self.op = _OP_ALIASES.get(op, op) if op else "="
        self.partial = partial

    def _lower_key(self):
        p = self.partial
        concrete = [n if n is not None else 0 for n in p.nums]
        if p.prerelease:
            from .version import _ident_key

            pre = (0, tuple(_ident_key(x) for x in p.prerelease))
        else:
            pre = _RELEASE
        return (concrete[0], concrete[1], concrete[2], pre), concrete

    def test(self, version):
        p = self.partial
        op = self.op
        precision = p.precision
        if precision == 0:
            # '*', 'x', empty: any version; >/</!= of "any" is unsatisfiable.
            return op in ("=", ">=", "<=", "~", "^")
        key = version.precedence_key()
        lower, concrete = self._lower_key()
        if op == "=":
            if precision == 3:
                return key == lower
            return lower <= key < _bump_key(concrete, precision)
        if op == "!=":
            if precision == 3:
                return key != lower
            return not (lower <= key < _bump_key(concrete, precision))
        if op == "<":
            return key < lower
        if op == "<=":
            if precision == 3:
                return key <= lower
            return key < _bump_key(concrete, precision)
        if op == ">":
            if precision == 3:
                return key > lower
            return key >= _bump_key(concrete, precision)
        if op == ">=":
            return key >= lower
        if op == "~":
            if precision >= 2:
                upper = (concrete[0], concrete[1] + 1, 0, _RELEASE)
            else:
                upper = (concrete[0] + 1, 0, 0, _RELEASE)
            return lower <= key < upper
        if op == "^":
            return lower <= key < _caret_upper_key(concrete, precision)
        raise AssertionError("unknown operator %r" % op)

    def __repr__(self):
        return "Comparator(%r, %r)" % (self.op, self.partial.nums)


def _parse_comparator(token, offset, source):
    m = _OP_PREFIX.match(token)
    if m:
        op = m.group(1)
        body = token[m.end() :]
        body_offset = offset + m.end()
    else:
        op = None
        body = token
        body_offset = offset
    if not body:
        raise RangeParseError(
            "missing version after %r" % (op or ""), body_offset, source
        )
    return Comparator(op, _parse_partial(body, body_offset, source))


class Range:
    """A parsed range expression: a disjunction of comparator sets."""

    __slots__ = ("sets", "source")

    def __init__(self, sets, source=""):
        self.sets = sets
        self.source = source

    @classmethod
    def parse(cls, text):
        if not isinstance(text, str):
            raise TypeError("range must be a str, got %s" % type(text).__name__)
        tokens = _tokenize(text)
        if not tokens:
            # Empty range matches everything (same convention as node-semver).
            return cls(((),), text)
        sets = []
        current = []
        last_or = -1
        for token, pos in tokens:
            if token == "||":
                if not current:
                    raise RangeParseError("empty comparator set", pos, text)
                sets.append(tuple(current))
                current = []
                last_or = pos
            else:
                current.append(_parse_comparator(token, pos, text))
        if not current:
            raise RangeParseError("empty comparator set", last_or, text)
        sets.append(tuple(current))
        return cls(tuple(sets), text)

    def match(self, version, include_prerelease=False):
        """True if ``version`` (a Version or string) satisfies the range.

        Versions carrying a prerelease tag only participate when
        ``include_prerelease`` is true.
        """
        if isinstance(version, str):
            version = Version.parse(version)
        if version.prerelease and not include_prerelease:
            return False
        return any(
            all(comp.test(version) for comp in comparator_set)
            for comparator_set in self.sets
        )

    def filter(self, versions, include_prerelease=False):
        return [v for v in versions if self.match(v, include_prerelease)]

    def max_satisfying(self, versions, include_prerelease=False):
        ok = self.filter(versions, include_prerelease)
        return max(ok, default=None)

    def __contains__(self, version):
        return self.match(version)

    def __str__(self):
        return self.source

    def __repr__(self):
        return "Range(%r)" % self.source


def match(range_text, version, include_prerelease=False):
    """Convenience one-shot: parse ``range_text`` and test ``version``."""
    return Range.parse(range_text).match(version, include_prerelease)
