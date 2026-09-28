"""保形插值库（仅标准库）。

核心概念
--------
- Kernel（核，可插拔）：决定区间内曲线形状。内置
    "linear" : 分段线性核（LinearKernel）
    "pchip"  : 保形分段三次核，Fritsch-Carlson / PCHIP（MonotoneCubicKernel）
  自定义核：继承 interp.Kernel 并实现 eval_segment(i, x)，需要时覆盖
  build / slope_lo / slope_hi，然后把类或实例传给 kernel= 参数；
  也可用 register_kernel 注册后按名字取用。
- Interpolator：统一插值器，负责排序、重复横坐标、外推与求值。
  逐点求值 f(x)；批量上采样 f.batch(grid) 或模块函数 upsample(xs, ys, grid)。

横坐标处理（duplicates 参数）：
    "error" : 发现重复横坐标直接抛 ValueError（默认）
    "last"  : 重复横坐标去重，同一 x 取最后出现的 y
    "first" : 重复横坐标去重，同一 x 取最先出现的 y
    无论哪种模式，输入都先按 x 升序排序，非等距采样不影响结果。

外推策略（extrapolate 参数）：
    "reject" : 超出 [x0, xn] 抛 ValueError
    "clamp"  : 超出范围时保持端点值（默认；保证永不引入新极值）
    "linear" : 用核在端点处的斜率线性外推（可能越出采样值范围）

单点输入：任何策略下都返回该点值（常数函数）。

批量一致性：batch/upsample 内部对每个网格点走与 __call__ 完全相同的
求值路径，因此结果与逐点调用逐元素相等（同一浮点结果，非近似）。
"""
from __future__ import annotations

import bisect
import math

from ._kernels import Kernel, LinearKernel, MonotoneCubicKernel

__all__ = [
    "Kernel",
    "Interpolator",
    "LinearInterpolator",
    "MonotoneCubicInterpolator",
    "LinearKernel",
    "MonotoneCubicKernel",
    "register_kernel",
    "available_kernels",
    "upsample",
]

_VALID_DUP = ("error", "last", "first")
_VALID_EXTRAP = ("reject", "clamp", "linear")

# 核注册表：名字 -> Kernel 子类
_KERNELS = {
    "linear": LinearKernel,
    "pchip": MonotoneCubicKernel,
    "monotone_cubic": MonotoneCubicKernel,  # 语义别名
}


def register_kernel(name, kernel_cls):
    """注册自定义核，之后可用 Interpolator(xs, ys, kernel=name) 按名字取用。

    kernel_cls 必须是 interp.Kernel 的子类（类本身，不是实例）。
    """
    if not isinstance(name, str) or not name:
        raise ValueError("核名字必须是非空字符串")
    if not (isinstance(kernel_cls, type) and issubclass(kernel_cls, Kernel)):
        raise TypeError("register_kernel 需要一个 Kernel 子类")
    _KERNELS[name] = kernel_cls


def available_kernels():
    """返回当前可用核名字的列表（含内置核与已注册自定义核）。"""
    return sorted(_KERNELS)


def _resolve_kernel(kernel):
    """把 kernel 参数解析为一个 Kernel 实例。

    接受：已注册名字（str）、Kernel 子类、Kernel 实例。
    """
    if isinstance(kernel, str):
        if kernel not in _KERNELS:
            raise ValueError(
                f"未知核 {kernel!r}；可用核: {available_kernels()}，"
                f"或传入 Kernel 子类/实例，或用 register_kernel 注册")
        return _KERNELS[kernel]()
    if isinstance(kernel, Kernel):
        return kernel
    if isinstance(kernel, type) and issubclass(kernel, Kernel):
        return kernel()
    raise TypeError(
        "kernel 必须是已注册核名字(str)、Kernel 子类或 Kernel 实例，"
        f"得到 {type(kernel).__name__}")


def _prepare(xs, ys, duplicates):
    """排序 + 处理重复横坐标，返回 (xs, ys) 升序且无重复。"""
    if duplicates not in _VALID_DUP:
        raise ValueError(f"duplicates 必须是 {_VALID_DUP} 之一, 得到 {duplicates!r}")
    try:
        xs = list(xs)
        ys = list(ys)
    except TypeError:
        raise TypeError("xs 与 ys 必须是可迭代的数值序列")
    if len(xs) != len(ys):
        raise ValueError("xs 与 ys 长度不一致")
    if len(xs) == 0:
        raise ValueError("至少需要一个采样点")
    pairs = sorted(zip(xs, ys), key=lambda p: p[0])
    for x, y in pairs:
        if not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
            raise TypeError("xs、ys 的元素必须是 int 或 float")
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("采样点含 NaN 或 inf")
    if len(pairs) == 1:
        return [pairs[0][0]], [pairs[0][1]]

    out_x, out_y = [pairs[0][0]], [pairs[0][1]]
    for x, y in pairs[1:]:
        if x == out_x[-1]:
            if duplicates == "error":
                raise ValueError(f"横坐标重复: x={x!r}")
            if duplicates == "last":
                out_y[-1] = y
            # "first": 保留已有值，忽略后来者
        else:
            out_x.append(x)
            out_y.append(y)
    return out_x, out_y


def _check_scalar(x):
    """校验逐点求值的横坐标，返回浮点化后的值。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise TypeError(f"逐点求值 x 必须是 int/float，得到 {type(x).__name__}")
    if not math.isfinite(x):
        raise ValueError(f"x 含 NaN 或 inf: {x!r}")
    return x


def _check_grid(grid):
    """校验批量目标网格，返回 list；拒绝标量/字符串，元素必须有限数值。"""
    if isinstance(grid, (str, bytes)) or not hasattr(grid, "__iter__"):
        raise TypeError(
            "目标网格必须是数值序列（list/tuple/...）；"
            f"逐点请用 f(x)，得到 {type(grid).__name__}")
    try:
        out = list(grid)
    except TypeError:
        raise TypeError("目标网格必须是可迭代的数值序列")
    for x in out:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise TypeError(f"目标网格元素必须是 int/float，得到 {type(x).__name__}")
        if not math.isfinite(x):
            raise ValueError(f"目标网格含 NaN 或 inf: {x!r}")
    return out


class Interpolator:
    """统一插值器：可插拔核 + 逐点/批量求值。

    参数
    ----
    xs, ys : 原始采样序列（不必排序、不必等距）
    kernel : "linear" / "pchip" / 已注册名字 / Kernel 子类 / Kernel 实例
    extrapolate : "clamp"（默认）/ "reject" / "linear"
    duplicates : "error"（默认）/ "last" / "first"
    """

    def __init__(self, xs, ys, *, kernel="pchip", extrapolate="clamp",
                 duplicates="error"):
        if extrapolate not in _VALID_EXTRAP:
            raise ValueError(
                f"extrapolate 必须是 {_VALID_EXTRAP} 之一, 得到 {extrapolate!r}")
        self._kernel = _resolve_kernel(kernel)
        self._kernel_name = getattr(self._kernel, "name", "custom")
        self._xs, self._ys = _prepare(xs, ys, duplicates)
        self._extrapolate = extrapolate
        if len(self._xs) >= 2:
            self._kernel.build(self._xs, self._ys)

    @property
    def xs(self):
        return list(self._xs)

    @property
    def ys(self):
        return list(self._ys)

    @property
    def kernel(self):
        """底层核实例（自定义核可借此访问其预计算状态）。"""
        return self._kernel

    def point(self, x):
        """单点求值，带完整参数校验。"""
        x = _check_scalar(x)
        return self._eval(x)

    def __call__(self, x):
        """逐点插值：f(x) -> 单个 float。批量请用 batch 或 upsample。"""
        return self.point(x)

    def _eval(self, x):
        """内部单点路径；调用前 x 已校验为有限数值。"""
        xs = self._xs
        if len(xs) == 1:
            return self._ys[0]
        if x < xs[0]:
            if self._extrapolate == "reject":
                raise ValueError(f"x={x!r} 超出插值范围 [{xs[0]!r}, {xs[-1]!r}]")
            if self._extrapolate == "clamp":
                return self._ys[0]
            return self._ys[0] + self._kernel.slope_lo() * (x - xs[0])
        if x > xs[-1]:
            if self._extrapolate == "reject":
                raise ValueError(f"x={x!r} 超出插值范围 [{xs[0]!r}, {xs[-1]!r}]")
            if self._extrapolate == "clamp":
                return self._ys[-1]
            return self._ys[-1] + self._kernel.slope_hi() * (x - xs[-1])
        i = bisect.bisect_right(xs, x) - 1
        if i >= len(xs) - 1:
            return self._ys[-1]
        return self._kernel.eval_segment(i, x)

    def batch(self, grid):
        """批量上采样：一次对整个目标网格求值，返回等长 list[float]。

        与 [f(x) for x in grid] 逐元素完全相等：每个网格点走同一个
        _eval 路径（同一份浮点运算），不做矢量化重写、不引入额外误差。
        """
        grid = _check_grid(grid)
        if len(self._xs) == 1:
            return [self._ys[0]] * len(grid)
        return [self._eval(x) for x in grid]

    # 兼容旧测试/旧用户：内部别名
    def _slope_lo(self):
        return self._kernel.slope_lo()

    def _slope_hi(self):
        return self._kernel.slope_hi()

    def __repr__(self):
        return (f"{type(self).__name__}(kernel={self._kernel_name!r}, "
                f"n={len(self._xs)}, range=[{self._xs[0]!r}, {self._xs[-1]!r}], "
                f"extrapolate={self._extrapolate!r})")


def upsample(xs, ys, grid, *, kernel="pchip", extrapolate="clamp",
             duplicates="error"):
    """批量上采样：原始采样序列 (xs, ys) + 目标网格 grid -> 插值结果 list。

    等价于 Interpolator(xs, ys, kernel=..., ...).batch(grid)，适合一次插出
    整条曲线。返回与 grid 等长的 list[float]，与逐点调用
    [Interpolator(xs, ys, ...)(x) for x in grid] 逐元素完全相等。
    """
    return Interpolator(xs, ys, kernel=kernel, extrapolate=extrapolate,
                        duplicates=duplicates).batch(grid)


class LinearInterpolator(Interpolator):
    """向后兼容：分段线性插值器（等价于 Interpolator(..., kernel="linear")）。"""

    def __init__(self, xs, ys, *, extrapolate="clamp", duplicates="error"):
        super().__init__(xs, ys, kernel="linear", extrapolate=extrapolate,
                         duplicates=duplicates)


class MonotoneCubicInterpolator(Interpolator):
    """向后兼容：保形分段三次插值器（等价于 Interpolator(..., kernel="pchip")）。"""

    def __init__(self, xs, ys, *, extrapolate="clamp", duplicates="error"):
        super().__init__(xs, ys, kernel="pchip", extrapolate=extrapolate,
                         duplicates=duplicates)
