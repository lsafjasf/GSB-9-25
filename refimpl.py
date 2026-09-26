"""Independent reference implementation used ONLY for differential testing.

It implements the same documented semantics as ``semverlib`` but is written
in a deliberately different style -- the official SemVer 2.0.0 regex for
parsing and interval-membership predicates for ranges -- so that bugs in
one implementation are unlikely to be mirrored in the other.

Only the standard library is used.
"""

import re

# Official semver.org 2.0.0 regex (adapted to named groups).
_VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

_REL = (1,)  # "no prerelease" marker; sorts above any prerelease


def parse_key(text):
    """Parse a version string into a comparable precedence key."""
    m = _VERSION_RE.match(text)
    if m is None:
        raise ValueError("invalid version: %r" % (text,))
    pre = m.group("pre")
    if pre is None:
        pre_key = _REL
    else:
        idents = tuple(
            (0, int(x)) if x.isdigit() else (1, x) for x in pre.split(".")
        )
        pre_key = (0, idents)
    return (int(m.group("major")), int(m.group("minor")), int(m.group("patch")), pre_key)


def has_prerelease(text):
    return _VERSION_RE.match(text).group("pre") is not None


def ref_cmp(a, b):
    """Three-way comparison of two version *strings* -> -1, 0 or 1."""
    ka, kb = parse_key(a), parse_key(b)
    return (ka > kb) - (ka < kb)


_OP_RE = re.compile(r"(<=|>=|==|!=|<|>|=|~|\^|!)?(.*)$", re.S)
_IDENT_RE = re.compile(r"[0-9A-Za-z-]+")
_NUM_RE = re.compile(r"0|[1-9]\d*")


def _check_idents(text, kind, strict_numeric):
    if text == "":
        raise ValueError("empty %s" % kind)
    for ident in text.split("."):
        if not _IDENT_RE.fullmatch(ident):
            raise ValueError("bad %s identifier %r" % (kind, ident))
        if (
            strict_numeric
            and ident.isdigit()
            and len(ident) > 1
            and ident.startswith("0")
        ):
            raise ValueError("leading zero in %s" % kind)


def _partial(body):
    """Parse a possibly-wildcard partial version.

    Returns (precision, concrete, pre_key) where concrete is a length-3
    list with wildcards/missing parts as 0, and pre_key is None unless a
    full X.Y.Z-prerelease was given.
    """
    core, plus, build = body.partition("+")
    if plus:
        _check_idents(build, "build", strict_numeric=False)
    core, dash, pre = core.partition("-")
    if dash:
        _check_idents(pre, "prerelease", strict_numeric=True)
    parts = core.split(".")
    if len(parts) > 3:
        raise ValueError("too many version parts in %r" % body)
    nums = []
    seen_wild = False
    for part in parts:
        if part in ("x", "X", "*"):
            nums.append(None)
            seen_wild = True
        elif _NUM_RE.fullmatch(part):
            if seen_wild:
                raise ValueError("number after wildcard in %r" % body)
            nums.append(int(part))
        else:
            raise ValueError("bad version part %r" % part)
    while len(nums) < 3:
        nums.append(None)
    precision = 0
    for n in nums:
        if n is None:
            break
        precision += 1
    if (dash or plus) and precision != 3:
        raise ValueError("wildcard version with prerelease/build: %r" % body)
    pre_key = None
    if dash:
        pre_key = (0, tuple((0, int(x)) if x.isdigit() else (1, x) for x in pre.split(".")))
    return precision, [0 if n is None else n for n in nums], pre_key


def _compile_comparator(token):
    """Compile one comparator token into a predicate over precedence keys."""
    m = _OP_RE.match(token)
    op = m.group(1) or "="
    op = {"==": "=", "!": "!="}.get(op, op)
    body = m.group(2)
    if body == "":
        raise ValueError("missing version in %r" % token)
    precision, c, pre_key = _partial(body)
    if precision == 0:
        if op in ("=", ">=", "<=", "~", "^"):
            return lambda key: True
        return lambda key: False
    low = (c[0], c[1], c[2], pre_key if pre_key is not None else _REL)

    def bump(prec):
        b = c[:prec]
        b[-1] += 1
        b += [0] * (3 - prec)
        return (b[0], b[1], b[2], _REL)

    if op == "=":
        if precision == 3:
            return lambda key: key == low
        hi = bump(precision)
        return lambda key: low <= key < hi
    if op == "!=":
        if precision == 3:
            return lambda key: key != low
        hi = bump(precision)
        return lambda key: not (low <= key < hi)
    if op == "<":
        return lambda key: key < low
    if op == "<=":
        if precision == 3:
            return lambda key: key <= low
        hi = bump(precision)
        return lambda key: key < hi
    if op == ">":
        if precision == 3:
            return lambda key: key > low
        hi = bump(precision)
        return lambda key: key >= hi
    if op == ">=":
        return lambda key: key >= low
    if op == "~":
        if precision >= 2:
            hi = (c[0], c[1] + 1, 0, _REL)
        else:
            hi = (c[0] + 1, 0, 0, _REL)
        return lambda key: low <= key < hi
    if op == "^":
        if c[0] != 0:
            hi = (c[0] + 1, 0, 0, _REL)
        elif precision == 1:
            hi = (1, 0, 0, _REL)
        elif c[1] != 0:
            hi = (0, c[1] + 1, 0, _REL)
        elif precision == 2:
            hi = (0, 1, 0, _REL)
        else:
            hi = (0, 0, c[2] + 1, _REL)
        return lambda key: low <= key < hi
    raise ValueError("unknown operator %r" % op)


def _parse_range_sets(text):
    if text.strip() == "":
        return [[]]
    sets = []
    for chunk in text.split("||"):
        tokens = chunk.split()
        if not tokens:
            raise ValueError("empty comparator set")
        sets.append([_compile_comparator(t) for t in tokens])
    return sets


def ref_match(range_text, version_text, include_prerelease=False):
    """Reference range matcher: True if version satisfies the range."""
    key = parse_key(version_text)
    if has_prerelease(version_text) and not include_prerelease:
        return False
    return any(
        all(pred(key) for pred in preds) for preds in _parse_range_sets(range_text)
    )
