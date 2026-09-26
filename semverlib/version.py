"""SemVer 2.0.0 version parsing and comparison.

Ordering rules (SemVer 2.0.0 section 11):

* ``major``, ``minor``, ``patch`` compare numerically.
* A version *with* a prerelease sorts *before* the same version without one
  (``1.0.0-alpha < 1.0.0``).
* Prerelease identifiers compare left to right: numeric identifiers compare
  numerically and are lower than alphanumeric ones; alphanumeric identifiers
  compare in ASCII order; a shorter identifier list is lower than a longer
  one when all preceding identifiers are equal.
* Build metadata (``+...``) is validated but does NOT participate in
  precedence: ``1.0.0+a == 1.0.0+b``.  ``Version.total_key()`` additionally
  uses build metadata as a deterministic tie-breaker, yielding a strict
  total order over distinct version strings.
"""

import functools

from .errors import VersionParseError

_DIGITS = frozenset("0123456789")
_IDENT_CHARS = frozenset(
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-"
)

# Key fragment marking "no prerelease"; sorts above any prerelease key.
_RELEASE = (1,)


def _is_ascii_digits(text):
    return bool(text) and all(c in _DIGITS for c in text)


def _ident_key(ident):
    if _is_ascii_digits(ident):
        return (0, int(ident))
    return (1, ident)


def _parse_ident_list(text, start, source, errcls, kind, strict_numeric, offset=0):
    """Parse a dot-separated identifier list starting at ``start``.

    Returns ``(idents, next_index)``.  For ``kind == 'prerelease'`` a ``+``
    terminates the list (build metadata follows).  Error positions are
    reported as ``offset + index``.
    """
    idents = []
    n = len(text)
    i = start
    while True:
        ident_start = i
        while i < n and text[i] in _IDENT_CHARS:
            i += 1
        ident = text[ident_start:i]
        if not ident:
            if ident_start < n and text[ident_start] not in ("."):
                raise errcls(
                    "invalid character %r in %s" % (text[ident_start], kind),
                    offset + ident_start,
                    source,
                )
            raise errcls("empty %s identifier" % kind, offset + ident_start, source)
        if (
            strict_numeric
            and _is_ascii_digits(ident)
            and len(ident) > 1
            and ident[0] == "0"
        ):
            raise errcls(
                "leading zero in numeric %s identifier" % kind,
                offset + ident_start,
                source,
            )
        idents.append(ident)
        if i >= n:
            return tuple(idents), i
        c = text[i]
        if c == ".":
            i += 1
            continue
        if c == "+" and kind == "prerelease":
            return tuple(idents), i
        raise errcls("invalid character %r in %s" % (c, kind), offset + i, source)


@functools.total_ordering
class Version:
    """An immutable SemVer 2.0.0 version.

    ``prerelease`` and ``build`` are tuples of identifier strings.
    Equality and ordering use *precedence* (build metadata ignored).
    """

    __slots__ = ("major", "minor", "patch", "prerelease", "build")

    def __init__(self, major, minor, patch, prerelease=(), build=()):
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = tuple(prerelease)
        self.build = tuple(build)

    @classmethod
    def parse(cls, text):
        """Parse a strict SemVer 2.0.0 string.

        Raises :class:`VersionParseError` (with the exact position) for
        anything else -- there is deliberately no fallback to string
        comparison or lenient parsing.
        """
        if not isinstance(text, str):
            raise TypeError("version must be a str, got %s" % type(text).__name__)
        n = len(text)
        i = 0
        nums = []
        for index, name in enumerate(("major", "minor", "patch")):
            run_start = i
            while i < n and text[i] in _DIGITS:
                i += 1
            run = text[run_start:i]
            if not run:
                raise VersionParseError(
                    "expected %s version number" % name, run_start, text
                )
            if len(run) > 1 and run[0] == "0":
                raise VersionParseError(
                    "leading zero in %s version" % name, run_start, text
                )
            nums.append(int(run))
            if index < 2:
                if i >= n or text[i] != ".":
                    raise VersionParseError(
                        "expected '.' after %s version" % name, i, text
                    )
                i += 1
        prerelease = ()
        build = ()
        if i < n and text[i] == "-":
            prerelease, i = _parse_ident_list(
                text, i + 1, text, VersionParseError, "prerelease", strict_numeric=True
            )
        if i < n and text[i] == "+":
            build, i = _parse_ident_list(
                text, i + 1, text, VersionParseError, "build", strict_numeric=False
            )
        if i < n:
            raise VersionParseError("unexpected character %r" % text[i], i, text)
        return cls(nums[0], nums[1], nums[2], prerelease, build)

    @property
    def is_prerelease(self):
        return bool(self.prerelease)

    def precedence_key(self):
        """Key implementing SemVer precedence (build metadata excluded)."""
        if self.prerelease:
            pre = (0, tuple(_ident_key(x) for x in self.prerelease))
        else:
            pre = _RELEASE
        return (self.major, self.minor, self.patch, pre)

    def total_key(self):
        """Strict-total-order key: precedence, then build metadata as a
        deterministic tie-breaker (library-specific, not SemVer precedence).
        """
        return self.precedence_key() + (tuple(_ident_key(x) for x in self.build),)

    def __eq__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self.precedence_key() == other.precedence_key()

    def __lt__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self.precedence_key() < other.precedence_key()

    def __hash__(self):
        return hash(self.precedence_key())

    def __str__(self):
        out = "%d.%d.%d" % (self.major, self.minor, self.patch)
        if self.prerelease:
            out += "-" + ".".join(self.prerelease)
        if self.build:
            out += "+" + ".".join(self.build)
        return out

    def __repr__(self):
        return "Version(%r)" % str(self)


def compare(a, b):
    """Three-way comparison of two :class:`Version` objects -> -1, 0 or 1."""
    ka = a.precedence_key()
    kb = b.precedence_key()
    return (ka > kb) - (ka < kb)
