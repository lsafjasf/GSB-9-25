"""Physical unit conversion library (Python 3, standard library only).

Units are declared in a JSON config file (see units.json); adding a new
unit never requires code changes.  Quantities carry a dimension vector
over the base dimensions declared in the config, so dimensionally
invalid arithmetic is rejected with both dimensions reported.
"""

from __future__ import annotations

import decimal
import json
import math
import re
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path


# ---------------------------------------------------------------- errors

class UnitError(Exception):
    """Base class for all errors raised by this library."""


class UnitSyntaxError(UnitError):
    """The unit expression is malformed (e.g. 'm//s', 'm**2')."""


class UnknownUnitError(UnitError):
    """The symbol is not a declared unit and matches no prefix+unit."""


class UnknownPrefixError(UnitError):
    """The symbol starts with a known prefix but the remainder is unknown."""


class RoundingSpecError(UnitError):
    """Conversion was requested without an explicit, valid rounding spec."""


class DimensionMismatchError(UnitError):
    """Operation on quantities with incompatible dimensions."""

    def __init__(self, op, left_dims, right_dims, dim_names):
        self.op = op
        self.left_dims = left_dims
        self.right_dims = right_dims
        left = format_dims(left_dims, dim_names)
        right = format_dims(right_dims, dim_names)
        super().__init__(
            f"dimension mismatch in {op}: [{left}] vs [{right}]"
        )


# ---------------------------------------------------------------- dims

def format_dims(dims, dim_names):
    """Human-readable dimension, e.g. 'L*M/T^2'; dimensionless -> '1'."""
    pos, neg = [], []
    for name, exp in zip(dim_names, dims):
        if exp > 0:
            pos.append(name if exp == 1 else f"{name}^{exp}")
        elif exp < 0:
            neg.append(name if exp == -1 else f"{name}^{-exp}")
    out = "*".join(pos) if pos else "1"
    if neg:
        out += "/" + "*".join(neg)
    return out


# ---------------------------------------------------------------- parsing

_TOKEN_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"|(?P<op>[*/])"
    r"|(?P<unit>[A-Za-z%μ°]+(?:\^[+-]?\d+|[+-]?\d+)?)"
)
_UNIT_TOKEN_RE = re.compile(r"^([A-Za-z%μ°]+)(?:\^?([+-]?\d+))?$")


def _tokenize(expr):
    tokens = []
    pos = 0
    while pos < len(expr):
        match = _TOKEN_RE.match(expr, pos)
        if match is None:
            raise UnitSyntaxError(
                f"cannot parse unit expression {expr!r} at position {pos}"
            )
        tokens.append((match.lastgroup, match.group()))
        pos = match.end()
    if not tokens:
        raise UnitSyntaxError("empty unit expression")
    return tokens


# ---------------------------------------------------------------- registry

class UnitRegistry:
    """Unit table loaded from a JSON config file."""

    def __init__(self, config_path):
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.dim_names = list(cfg["dimensions"])
        self._zero = (0,) * len(self.dim_names)
        self.prefixes = sorted(
            ((p, Fraction(f)) for p, f in cfg["prefixes"].items()),
            key=lambda kv: -len(kv[0]),
        )
        self.units = {}
        for symbol, spec in cfg["units"].items():
            if "dim" in spec:
                dims = self._base_dims(spec["dim"])
                factor = Fraction(spec.get("factor", "1"))
            else:
                factor, dims = self._eval(spec["define"])
            self.units[symbol] = (factor, dims)
        self._expr_cache = {}

    def _base_dims(self, dim):
        if dim == "":
            return self._zero
        if dim not in self.dim_names:
            raise UnitError(f"config references unknown dimension {dim!r}")
        return tuple(1 if n == dim else 0 for n in self.dim_names)

    # -- unit symbol resolution --------------------------------------

    def resolve(self, symbol):
        """Resolve a bare symbol (no exponent) to (factor, dims)."""
        if symbol in self.units:
            return self.units[symbol]
        for prefix, pfactor in self.prefixes:
            if symbol.startswith(prefix) and len(symbol) > len(prefix):
                rest = symbol[len(prefix):]
                if rest in self.units:
                    factor, dims = self.units[rest]
                    return pfactor * factor, dims
        for prefix, _ in self.prefixes:
            if symbol.startswith(prefix) and len(symbol) > len(prefix):
                raise UnknownPrefixError(
                    f"unknown unit {symbol[len(prefix):]!r} after prefix "
                    f"{prefix!r} in symbol {symbol!r}"
                )
        raise UnknownUnitError(f"unknown unit: {symbol!r}")

    # -- expression evaluation ----------------------------------------

    def parse(self, expr):
        """Parse a unit expression like 'kg*m/s^2' -> (factor, dims)."""
        expr = expr.strip()
        cached = self._expr_cache.get(expr)
        if cached is None:
            cached = self._eval(expr)
            self._expr_cache[expr] = cached
        return cached

    def _eval(self, expr):
        expr = re.sub(r"\s+", "", expr)
        tokens = _tokenize(expr)
        if tokens[0][0] == "op":
            raise UnitSyntaxError(f"expression {expr!r} starts with an operator")

        factor = Fraction(1)
        dims = self._zero
        divide = False
        expect_term = True
        for kind, text in tokens:
            if expect_term:
                if kind == "op":
                    raise UnitSyntaxError(
                        f"unexpected operator {text!r} in expression {expr!r}"
                    )
                tfactor, tdims = self._eval_term(kind, text, expr)
                if divide:
                    factor /= tfactor
                    dims = tuple(a - b for a, b in zip(dims, tdims))
                else:
                    factor *= tfactor
                    dims = tuple(a + b for a, b in zip(dims, tdims))
                expect_term = False
            else:
                if kind != "op":
                    raise UnitSyntaxError(
                        f"missing operator before {text!r} in expression {expr!r}"
                    )
                divide = text == "/"
                expect_term = True
        if expect_term:
            raise UnitSyntaxError(f"expression {expr!r} ends with an operator")
        return factor, dims

    def _eval_term(self, kind, text, expr):
        if kind == "num":
            return Fraction(text), self._zero
        match = _UNIT_TOKEN_RE.match(text)
        if match is None:
            raise UnitSyntaxError(f"bad unit token {text!r} in expression {expr!r}")
        symbol, exp_text = match.groups()
        exp = int(exp_text) if exp_text else 1
        factor, dims = self.resolve(symbol)
        return factor ** exp, tuple(e * exp for e in dims)

    # -- quantity construction ----------------------------------------

    def quantity(self, value, unit=""):
        """Create a Quantity from a numeric value and a unit expression."""
        if unit == "":
            return Quantity(float(value), self._zero, self)
        factor, dims = self.parse(unit)
        return Quantity(float(value) * float(factor), dims, self)

    def parse_quantity(self, text):
        """Parse '3.6 km/h' into a Quantity."""
        parts = text.strip().split(None, 1)
        if len(parts) != 2:
            raise UnitSyntaxError(f"expected '<number> <unit>', got {text!r}")
        return self.quantity(float(parts[0]), parts[1])


# ---------------------------------------------------------------- rounding

_ROUNDING_MODES = {
    name for name in dir(decimal) if name.startswith("ROUND_")
}


def _round_sig_figs(d, sig_figs, mode):
    if d.is_zero():
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = max(60, sig_figs + 10)
        quantum = Decimal(1).scaleb(d.adjusted() - sig_figs + 1)
        return d.quantize(quantum, rounding=mode)


def _round_decimals(d, decimals, mode):
    with localcontext() as ctx:
        ctx.prec = max(60, len(d.as_integer_ratio()[0].__str__()) + decimals + 10)
        return d.quantize(Decimal(1).scaleb(-decimals), rounding=mode)


# ---------------------------------------------------------------- quantity

class Quantity:
    """A physical value stored in coherent base units plus a dimension vector."""

    __slots__ = ("_si", "dims", "_reg")
    __hash__ = None  # __eq__ may raise; do not use as dict keys

    def __init__(self, si_value, dims, registry):
        self._si = float(si_value)
        self.dims = dims
        self._reg = registry

    # -- helpers ------------------------------------------------------

    def _check(self, other, op):
        if not isinstance(other, Quantity):
            raise TypeError(f"cannot {op} Quantity and {type(other).__name__}")
        if self.dims != other.dims:
            raise DimensionMismatchError(
                op, self.dims, other.dims, self._reg.dim_names
            )

    @property
    def si_value(self):
        """Value in coherent base units (float)."""
        return self._si

    def is_dimensionless(self):
        return all(e == 0 for e in self.dims)

    # -- arithmetic -----------------------------------------------------

    def __add__(self, other):
        self._check(other, "addition")
        return Quantity(self._si + other._si, self.dims, self._reg)

    def __sub__(self, other):
        self._check(other, "subtraction")
        return Quantity(self._si - other._si, self.dims, self._reg)

    def __mul__(self, other):
        if isinstance(other, Quantity):
            dims = tuple(a + b for a, b in zip(self.dims, other.dims))
            return Quantity(self._si * other._si, dims, self._reg)
        if isinstance(other, (int, float)):
            return Quantity(self._si * other, self.dims, self._reg)
        return NotImplemented

    __rmul__ = __mul__

    def __truediv__(self, other):
        if isinstance(other, Quantity):
            dims = tuple(a - b for a, b in zip(self.dims, other.dims))
            return Quantity(self._si / other._si, dims, self._reg)
        if isinstance(other, (int, float)):
            return Quantity(self._si / other, self.dims, self._reg)
        return NotImplemented

    def __neg__(self):
        return Quantity(-self._si, self.dims, self._reg)

    # -- comparison -----------------------------------------------------

    def __eq__(self, other):
        if not isinstance(other, Quantity):
            return NotImplemented
        self._check(other, "comparison")
        return math.isclose(self._si, other._si, rel_tol=1e-9, abs_tol=0.0)

    def __lt__(self, other):
        self._check(other, "comparison")
        return self._si < other._si

    def __le__(self, other):
        self._check(other, "comparison")
        return self._si <= other._si

    def __gt__(self, other):
        self._check(other, "comparison")
        return self._si > other._si

    def __ge__(self, other):
        self._check(other, "comparison")
        return self._si >= other._si

    # -- conversion -----------------------------------------------------

    def to(self, target, *, sig_figs=None, decimals=None,
           rounding="ROUND_HALF_EVEN"):
        """Convert to `target` unit with an explicit rounding specification.

        Exactly one of `sig_figs` / `decimals` must be given; `rounding`
        names a decimal-module rounding mode (default ROUND_HALF_EVEN,
        i.e. banker's rounding).  Returns a ConversionResult.
        """
        factor, dims = self._reg.parse(target)
        if dims != self.dims:
            raise DimensionMismatchError(
                "conversion", self.dims, dims, self._reg.dim_names
            )
        if (sig_figs is None) == (decimals is None):
            raise RoundingSpecError(
                "specify exactly one of sig_figs= or decimals="
            )
        if rounding not in _ROUNDING_MODES:
            raise RoundingSpecError(f"unknown rounding mode {rounding!r}")
        if sig_figs is not None and (not isinstance(sig_figs, int) or sig_figs < 1):
            raise RoundingSpecError("sig_figs must be a positive integer")
        if decimals is not None and (not isinstance(decimals, int) or decimals < 0):
            raise RoundingSpecError("decimals must be a non-negative integer")

        with localcontext() as ctx:
            ctx.prec = 80
            raw = (
                Decimal(str(self._si))
                * Decimal(factor.denominator)
                / Decimal(factor.numerator)
            )
        if sig_figs is not None:
            rounded = _round_sig_figs(raw, sig_figs, rounding)
            spec = f"{sig_figs} significant figures, {rounding}"
        else:
            rounded = _round_decimals(raw, decimals, rounding)
            spec = f"{decimals} decimal places, {rounding}"
        return ConversionResult(rounded, target, spec, factor, dims, self._reg)

    def __repr__(self):
        dims = format_dims(self.dims, self._reg.dim_names)
        return f"Quantity({self._si!r} [{dims}])"


class ConversionResult:
    """Result of Quantity.to(): a rounded Decimal plus full provenance."""

    __slots__ = ("value", "unit", "rounding", "_factor", "_dims", "_reg")

    def __init__(self, value, unit, rounding, factor, dims, registry):
        self.value = value          # Decimal, already rounded
        self.unit = unit            # target unit expression
        self.rounding = rounding    # human-readable rounding spec
        self._factor = factor
        self._dims = dims
        self._reg = registry

    @property
    def quantity(self):
        """Back-convert to a Quantity (for round-trip checks)."""
        return Quantity(
            float(self.value) * float(self._factor), self._dims, self._reg
        )

    def __repr__(self):
        return f"{self.value} {self.unit} ({self.rounding})"

    def __str__(self):
        return f"{self.value} {self.unit}"


# ---------------------------------------------------------------- default

_DEFAULT_REGISTRY = None


def default_registry():
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = UnitRegistry(Path(__file__).with_name("units.json"))
    return _DEFAULT_REGISTRY


def Q(value, unit=""):
    """Shorthand: Q(3.6, 'km/h') using the default units.json registry."""
    return default_registry().quantity(value, unit)
