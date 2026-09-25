"""保形插值库（仅标准库）。

提供：
- LinearInterpolator        分段线性插值
- MonotoneCubicInterpolator 分段三次保形插值（Fritsch-Carlson / PCHIP）

共同约定
--------
横坐标处理（duplicates 参数）：
    "error" : 发现重复横坐标直接抛 ValueError（默认）
    "last"  : 重复横坐标去重，同一 x 取最后出现的 y
    "first" : 重复横坐标去重，同一 x 取最先出现的 y
    无论哪种模式，输入都先按 x 升序排序，非等距采样不影响结果。

外推策略（extrapolate 参数）：
    "reject" : 超出 [x0, xn] 抛 ValueError
    "clamp"  : 超出范围时保持端点值（默认；保证永不引入新极值）
    "linear" : 用端点处切线斜率线性外推（可能越出采样值范围）

单点输入：任何策略下都返回该点值（常数函数）。
"""
from __future__ import annotations

import bisect
import math

__all__ = ["LinearInterpolator", "MonotoneCubicInterpolator"]

_VALID_DUP = ("error", "last", "first")
_VALID_EXTRAP = ("reject", "clamp", "linear")


def _prepare(xs, ys, duplicates):
    """排序 + 处理重复横坐标，返回 (xs, ys) 升序且无重复。"""
    if duplicates not in _VALID_DUP:
        raise ValueError(f"duplicates 必须是 {_VALID_DUP} 之一, 得到 {duplicates!r}")
    if len(xs) != len(ys):
        raise ValueError("xs 与 ys 长度不一致")
    if len(xs) == 0:
        raise ValueError("至少需要一个采样点")
    pairs = sorted(zip(xs, ys), key=lambda p: p[0])
    for x, y in pairs:
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


class _Base:
    def __init__(self, xs, ys, *, extrapolate="clamp", duplicates="error"):
        if extrapolate not in _VALID_EXTRAP:
            raise ValueError(f"extrapolate 必须是 {_VALID_EXTRAP} 之一, 得到 {extrapolate!r}")
        self._xs, self._ys = _prepare(list(xs), list(ys), duplicates)
        self._extrapolate = extrapolate
        self._build()

    # 子类钩子：构造内部系数；在给定区间 i 上求值；端点外推斜率
    def _build(self):
        pass

    def _eval_segment(self, i, x):
        raise NotImplementedError

    def _slope_lo(self):
        raise NotImplementedError

    def _slope_hi(self):
        raise NotImplementedError

    @property
    def xs(self):
        return list(self._xs)

    @property
    def ys(self):
        return list(self._ys)

    def __call__(self, x):
        xs = self._xs
        if len(xs) == 1:
            return self._ys[0]
        if x < xs[0]:
            if self._extrapolate == "reject":
                raise ValueError(f"x={x!r} 超出插值范围 [{xs[0]!r}, {xs[-1]!r}]")
            if self._extrapolate == "clamp":
                return self._ys[0]
            return self._ys[0] + self._slope_lo() * (x - xs[0])
        if x > xs[-1]:
            if self._extrapolate == "reject":
                raise ValueError(f"x={x!r} 超出插值范围 [{xs[0]!r}, {xs[-1]!r}]")
            if self._extrapolate == "clamp":
                return self._ys[-1]
            return self._ys[-1] + self._slope_hi() * (x - xs[-1])
        i = bisect.bisect_right(xs, x) - 1
        if i >= len(xs) - 1:
            return self._ys[-1]
        return self._eval_segment(i, x)

    def __repr__(self):
        return (f"{type(self).__name__}(n={len(self._xs)}, "
                f"range=[{self._xs[0]!r}, {self._xs[-1]!r}], "
                f"extrapolate={self._extrapolate!r})")


class LinearInterpolator(_Base):
    """分段线性插值。天然不越出相邻采样点范围、保持单调性。"""

    def _eval_segment(self, i, x):
        x0, x1 = self._xs[i], self._xs[i + 1]
        y0, y1 = self._ys[i], self._ys[i + 1]
        t = (x - x0) / (x1 - x0)
        return y0 + t * (y1 - y0)

    def _slope_lo(self):
        return (self._ys[1] - self._ys[0]) / (self._xs[1] - self._xs[0])

    def _slope_hi(self):
        return (self._ys[-1] - self._ys[-2]) / (self._xs[-1] - self._xs[-2])


class MonotoneCubicInterpolator(_Base):
    """Fritsch-Carlson 保形分段三次 Hermite 插值（PCHIP）。

    每个区间是三次 Hermite 样条，节点斜率按 Fritsch-Carlson 规则选取：
    相邻割线异号（局部极值）时斜率取 0，否则取割线的加权调和平均。
    保证：
      1. 每个区间内的值不越出该区间两端采样点的范围（零过冲）；
      2. 采样点单调（非降/非升）时插值结果同样单调。
    """

    def _build(self):
        xs, ys = self._xs, self._ys
        n = len(xs)
        if n < 2:
            self._m = [0.0]
            return
        h = [xs[i + 1] - xs[i] for i in range(n - 1)]
        d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
        m = [0.0] * n
        # 内部节点：Fritsch-Carlson
        for i in range(1, n - 1):
            if d[i - 1] * d[i] <= 0.0:
                m[i] = 0.0
            else:
                w1 = 2.0 * h[i] + h[i - 1]
                w2 = h[i] + 2.0 * h[i - 1]
                m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
        # 端点：非中心三点公式 + 限幅（PCHIP 标准做法）
        m[0] = self._edge_slope(h[0], h[1], d[0], d[1]) if n > 2 else d[0]
        m[-1] = self._edge_slope(h[-1], h[-2], d[-1], d[-2]) if n > 2 else d[-1]
        self._m = m

    @staticmethod
    def _edge_slope(h0, h1, d0, d1):
        m = ((2.0 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if m * d0 <= 0.0:
            return 0.0
        if d0 * d1 < 0.0 and abs(m) > 3.0 * abs(d0):
            return 3.0 * d0
        return m

    def _eval_segment(self, i, x):
        x0, x1 = self._xs[i], self._xs[i + 1]
        y0, y1 = self._ys[i], self._ys[i + 1]
        m0, m1 = self._m[i], self._m[i + 1]
        h = x1 - x0
        t = (x - x0) / h
        t2, t3 = t * t, t * t * t
        h00 = 2.0 * t3 - 3.0 * t2 + 1.0
        h10 = t3 - 2.0 * t2 + t
        h01 = -2.0 * t3 + 3.0 * t2
        h11 = t3 - t2
        return h00 * y0 + h10 * h * m0 + h01 * y1 + h11 * h * m1

    def _slope_lo(self):
        return self._m[0]

    def _slope_hi(self):
        return self._m[-1]
