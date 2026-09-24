"""物理单位换算库（仅标准库）。

- 单位表由 units.json 声明，新增单位无需改代码。
- 支持基本单位、复合单位表达式（* / ^）、十倍幂前缀。
- 量纲不一致的加减/比较抛 DimensionMismatchError（报出两侧量纲）。
- 换算必须显式指定 sig_figs 或 places 之一，并指定舍入方式。
"""

from __future__ import annotations

import json
import math
import os
import re
from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
)

# ---------------------------------------------------------------- 错误类型

class UnitError(Exception):
    """所有单位相关错误的基类。"""


class UnknownUnitError(UnitError):
    """符号不是已知单位，也无法拆成 前缀+单位。"""


class UnknownPrefixError(UnitError):
    """尾部是合法单位，但头部不是已知前缀。"""


class InvalidUnitExpressionError(UnitError):
    """单位表达式语法非法。"""


class DimensionMismatchError(UnitError):
    """加减/比较时两侧量纲不一致，消息中报出两侧量纲。"""

    def __init__(self, left_dims, right_dims, op):
        self.left_dims = left_dims
        self.right_dims = right_dims
        super().__init__(
            "dimension mismatch in %s: %s vs %s"
            % (op, format_dims(left_dims), format_dims(right_dims))
        )


class ConversionTargetMismatchError(UnitError):
    """换算目标单位与源量纲不匹配。"""

    def __init__(self, src_dims, target_unit, target_dims):
        super().__init__(
            "cannot convert %s to unit %r (dims %s)"
            % (format_dims(src_dims), target_unit, format_dims(target_dims))
        )


# ---------------------------------------------------------------- 量纲

_DIM_SYMBOLS = ("L", "M", "T", "I", "K", "N", "J")
_DIM_NAMES = {
    "L": "length", "M": "mass", "T": "time", "I": "current",
    "K": "temperature", "N": "amount", "J": "luminous_intensity",
}
_ZERO_DIMS = (0.0,) * len(_DIM_SYMBOLS)


def _clean(exp):
    r = round(exp, 9)
    return 0.0 if r == 0 else r


def format_dims(dims):
    """把量纲元组格式化为可读字符串，如 'L·M·T^-2'；无量纲返回 '1'。"""
    parts = []
    for sym, exp in zip(_DIM_SYMBOLS, dims):
        if exp == 0:
            continue
        if exp == 1:
            parts.append(sym)
        else:
            e = int(exp) if float(exp).is_integer() else exp
            parts.append("%s^%s" % (sym, e))
    return "·".join(parts) if parts else "1"


# ---------------------------------------------------------------- 舍入

_ROUNDING_MODES = {
    "half_even": ROUND_HALF_EVEN,   # 银行家舍入（默认，无系统偏差）
    "half_up": ROUND_HALF_UP,       # 四舍五入
    "down": ROUND_DOWN,             # 向零截断
    "up": ROUND_UP,                 # 远离零
    "floor": ROUND_FLOOR,           # 向负无穷
    "ceiling": ROUND_CEILING,       # 向正无穷
}


def apply_rounding(value, sig_figs=None, places=None, rounding="half_even"):
    """按有效数字（sig_figs）或小数位（places）舍入，二者必须且只能给一个。"""
    if (sig_figs is None) == (places is None):
        raise ValueError("exactly one of sig_figs / places must be given")
    if rounding not in _ROUNDING_MODES:
        raise ValueError("unknown rounding mode %r, expect one of %s"
                         % (rounding, sorted(_ROUNDING_MODES)))
    mode = _ROUNDING_MODES[rounding]
    d = Decimal(str(value))
    if sig_figs is not None:
        if sig_figs < 1:
            raise ValueError("sig_figs must be >= 1")
        if d.is_zero():
            return 0.0
        quantum = Decimal(1).scaleb(d.adjusted() - sig_figs + 1)
    else:
        quantum = Decimal(1).scaleb(-places)
    return float(d.quantize(quantum, rounding=mode))


# ---------------------------------------------------------------- 单位注册表

_FACTOR_RE = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_SYMBOL_RE = re.compile(r"([^\W\d_]+|µ)(?:\^([+-]?\d+(?:\.\d+)?))?")


class UnitRegistry:
    """由配置构建的单位注册表。"""

    def __init__(self, config):
        self.prefixes = dict(config.get("prefixes", {}))
        self.units = {}  # symbol -> (dims tuple, factor to canonical)
        for sym, spec in config.get("base", {}).items():
            if isinstance(spec, str):
                spec = {"dim": spec, "factor": 1.0}
            dims = [0.0] * len(_DIM_SYMBOLS)
            dims[_DIM_SYMBOLS.index(spec["dim"])] = 1.0
            self.units[sym] = (tuple(dims), float(spec.get("factor", 1.0)))
        # 多趟解析：derived 可引用任意其他 derived，与声明顺序无关
        pending = dict(config.get("derived", {}))
        while pending:
            progressed = False
            for sym, expr in list(pending.items()):
                try:
                    self.units[sym] = self.parse(expr)
                except UnitError:
                    continue
                del pending[sym]
                progressed = True
            if not progressed:
                sym, expr = next(iter(pending.items()))
                self.parse(expr)  # 抛出真正的错误


    def resolve_symbol(self, sym):
        """解析单个符号：先精确匹配单位，再尝试 前缀+单位。"""
        if sym in self.units:
            return self.units[sym]
        # 收集所有“尾部是已知单位”的拆分，优先取前缀合法者
        bad_heads = []
        for i in range(1, len(sym)):
            head, tail = sym[:i], sym[i:]
            if tail not in self.units:
                continue
            if head in self.prefixes:
                dims, factor = self.units[tail]
                return dims, factor * self.prefixes[head]
            bad_heads.append(head)
        if bad_heads:
            raise UnknownPrefixError(
                "unknown prefix %r in unit symbol %r" % (bad_heads[0], sym))
        raise UnknownUnitError("unknown unit %r" % sym)

    def parse(self, expr):
        """解析单位表达式，如 'kg*m/s^2'、'km/h'、'0.001*m^3'、'1'。

        返回 (dims, factor)。
        """
        expr = expr.strip()
        if expr in ("", "1"):
            return _ZERO_DIMS, 1.0
        dims = [0.0] * len(_DIM_SYMBOLS)
        factor = 1.0
        sign = 1.0
        for tok in re.split(r"([*/])", expr):
            tok = tok.strip()
            if not tok:
                continue
            if tok == "*":
                sign = 1.0
                continue
            if tok == "/":
                sign = -1.0
                continue
            if _FACTOR_RE.fullmatch(tok):
                factor *= float(tok) ** sign
                continue
            m = _SYMBOL_RE.fullmatch(tok)
            if not m:
                raise InvalidUnitExpressionError(
                    "invalid token %r in unit expression %r" % (tok, expr))
            name, exp = m.group(1), m.group(2)
            d, f = self.resolve_symbol(name)
            e = (float(exp) if exp else 1.0) * sign
            for i in range(len(dims)):
                dims[i] += d[i] * e
            factor *= f ** e
        return tuple(_clean(x) for x in dims), factor


_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "units.json")


def load_registry(path=None):
    with open(path or _DEFAULT_CONFIG, encoding="utf-8") as fh:
        return UnitRegistry(json.load(fh))


DEFAULT_REGISTRY = load_registry()

# ---------------------------------------------------------------- 量

class Quantity:
    """一个带量纲的物理量，内部以 canonical（SI）值存储。"""

    __slots__ = ("value", "dims", "_registry")

    def __init__(self, value, unit="1", registry=None):
        self._registry = registry or DEFAULT_REGISTRY
        dims, factor = self._registry.parse(unit)
        self.dims = dims
        self.value = float(value) * factor

    # -- 内部构造 --
    @classmethod
    def _from_canonical(cls, value, dims, registry):
        q = cls.__new__(cls)
        q.value = float(value)
        q.dims = dims
        q._registry = registry
        return q

    # -- 换算 --
    def to(self, unit, sig_figs=None, places=None, rounding="half_even"):
        """换算到目标单位。必须显式给出 sig_figs 或 places 及舍入方式。

        量纲不匹配抛 ConversionTargetMismatchError，绝不默认按比例换算。
        """
        dims, factor = self._registry.parse(unit)
        if dims != self.dims:
            raise ConversionTargetMismatchError(self.dims, unit, dims)
        raw = self.value / factor
        return apply_rounding(raw, sig_figs=sig_figs, places=places,
                              rounding=rounding)

    @property
    def is_dimensionless(self):
        return self.dims == _ZERO_DIMS

    # -- 运算 --
    def _check_dims(self, other, op):
        if not isinstance(other, Quantity):
            raise TypeError("cannot %s Quantity and %r" % (op, type(other)))
        if self.dims != other.dims:
            raise DimensionMismatchError(self.dims, other.dims, op)

    def __add__(self, other):
        self._check_dims(other, "addition")
        return Quantity._from_canonical(self.value + other.value, self.dims,
                                        self._registry)

    def __sub__(self, other):
        self._check_dims(other, "subtraction")
        return Quantity._from_canonical(self.value - other.value, self.dims,
                                        self._registry)

    def __mul__(self, other):
        if isinstance(other, (int, float)):
            return Quantity._from_canonical(self.value * other, self.dims,
                                            self._registry)
        if not isinstance(other, Quantity):
            return NotImplemented
        dims = tuple(_clean(a + b) for a, b in zip(self.dims, other.dims))
        return Quantity._from_canonical(self.value * other.value, dims,
                                        self._registry)

    __rmul__ = __mul__

    def __truediv__(self, other):
        if isinstance(other, (int, float)):
            return Quantity._from_canonical(self.value / other, self.dims,
                                            self._registry)
        if not isinstance(other, Quantity):
            return NotImplemented
        dims = tuple(_clean(a - b) for a, b in zip(self.dims, other.dims))
        return Quantity._from_canonical(self.value / other.value, dims,
                                        self._registry)

    def __pow__(self, n):
        dims = tuple(_clean(a * n) for a in self.dims)
        return Quantity._from_canonical(self.value ** n, dims, self._registry)

    # -- 比较 --
    def _cmp(self, other, op, fn):
        if isinstance(other, (int, float)) and self.is_dimensionless:
            return fn(self.value, other)
        self._check_dims(other, op)
        return fn(self.value, other.value)

    def __eq__(self, other):
        if isinstance(other, (int, float)) and self.is_dimensionless:
            return math.isclose(self.value, other, rel_tol=1e-9, abs_tol=0.0)
        if not isinstance(other, Quantity):
            return NotImplemented
        if self.dims != other.dims:
            return False
        return math.isclose(self.value, other.value, rel_tol=1e-9, abs_tol=0.0)

    def __lt__(self, other):
        return self._cmp(other, "comparison '<'", lambda a, b: a < b)

    def __le__(self, other):
        return self._cmp(other, "comparison '<='", lambda a, b: a <= b)

    def __gt__(self, other):
        return self._cmp(other, "comparison '>'", lambda a, b: a > b)

    def __ge__(self, other):
        return self._cmp(other, "comparison '>='", lambda a, b: a >= b)

    __hash__ = None

    def __repr__(self):
        return "Quantity(%r, dims=%s)" % (self.value, format_dims(self.dims))


def convert(value, from_unit, to_unit, sig_figs=None, places=None,
            rounding="half_even", registry=None):
    """便捷函数：把 value（from_unit）换算到 to_unit，并按指定精度舍入。"""
    return Quantity(value, from_unit, registry=registry).to(
        to_unit, sig_figs=sig_figs, places=places, rounding=rounding)
