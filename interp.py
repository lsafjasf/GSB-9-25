"""interp: 线性插值与保形（单调）分段三次插值（PCHIP, Fritsch-Carlson）。

仅使用 Python 标准库。

设计规则（明确约定）
--------------------
1. 横坐标重复（duplicate x）：
   默认 ``on_duplicate='error'``，发现重复横坐标立即抛出 ``ValueError``；
   可选 ``on_duplicate='last'``，同一 x 出现多次时取**最后一个** y 值。
2. 外推策略 ``extrapolate``：
   - ``'error'``（默认）：查询点超出 [x0, xn] 时抛出 ``ValueError``；
   - ``'clamp'``：超出范围时保持端点值（返回 y0 或 yn）；
   - ``'linear'``：用端点处的斜率做线性外推
     （线性插值用首/末段斜率；PCHIP 用端点导数 m0 / mn）。
   极端延伸下的表现：
   - ``'error'``  任何越界都报错，绝不产生未经验证的值；
   - ``'clamp'``  值永远被夹在端点值上，不会发散，但远距离处完全失真；
   - ``'linear'`` 沿端点切线无限延伸，|x| 很大时值会无界发散，
     只应在紧邻采样区间的短距离外推时使用。
3. 保形性：PCHIP 在每个区间 [x_k, x_{k+1}] 上的值不超出
   [min(y_k, y_{k+1}), max(y_k, y_{k+1})]（过冲量为零），
   且 y 单调的区间上插值结果保持同样的单调方向。
"""

from bisect import bisect_right

__all__ = ["LinearInterpolator", "PchipInterpolator", "make_interpolator"]

_EXTRAPOLATE_MODES = ("error", "clamp", "linear")
_DUPLICATE_MODES = ("error", "last")


def _prepare_xy(x, y, on_duplicate):
    """校验并预处理采样点：排序、查重、有限性检查。"""
    x = [float(v) for v in x]
    y = [float(v) for v in y]
    if len(x) != len(y):
        raise ValueError("x 与 y 长度必须一致，got %d vs %d" % (len(x), len(y)))
    if len(x) == 0:
        raise ValueError("至少需要一个采样点")
    for v in x + y:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("采样点包含 NaN 或无穷值: %r" % (v,))

    order = sorted(range(len(x)), key=lambda i: x[i])
    xs = [x[i] for i in order]
    ys = [y[i] for i in order]

    if on_duplicate == "error":
        for i in range(1, len(xs)):
            if xs[i] == xs[i - 1]:
                raise ValueError(
                    "横坐标重复: x=%r 出现多次（可用 on_duplicate='last' 取最后一个）"
                    % (xs[i],)
                )
    else:  # 'last'：同一 x 保留最后出现的 y
        dedup_x = [xs[0]]
        dedup_y = [ys[0]]
        for i in range(1, len(xs)):
            if xs[i] == xs[i - 1]:
                dedup_y[-1] = ys[i]
            else:
                dedup_x.append(xs[i])
                dedup_y.append(ys[i])
        xs, ys = dedup_x, dedup_y
    return xs, ys


class _BaseInterpolator:
    """公共骨架：预处理、定位区间、外推分发。"""

    def __init__(self, x, y, extrapolate="error", on_duplicate="error"):
        if extrapolate not in _EXTRAPOLATE_MODES:
            raise ValueError("extrapolate 必须是 %r 之一" % (_EXTRAPOLATE_MODES,))
        if on_duplicate not in _DUPLICATE_MODES:
            raise ValueError("on_duplicate 必须是 %r 之一" % (_DUPLICATE_MODES,))
        self.x, self.y = _prepare_xy(x, y, on_duplicate)
        self.extrapolate = extrapolate

    @property
    def n(self):
        return len(self.x)

    def __call__(self, t):
        if isinstance(t, (list, tuple)):
            return [self._eval_one(float(v)) for v in t]
        return self._eval_one(float(t))

    def _eval_one(self, t):
        x, y = self.x, self.y
        if t < x[0]:
            return self._extrapolate_left(t)
        if t > x[-1]:
            return self._extrapolate_right(t)
        # 区间内（含端点）
        k = bisect_right(x, t) - 1
        if k >= len(x) - 1:
            return y[-1]
        return self._eval_segment(k, t)

    def _extrapolate_left(self, t):
        if self.extrapolate == "error":
            raise ValueError(
                "查询点 %r 小于最小横坐标 %r，且 extrapolate='error'"
                % (t, self.x[0])
            )
        if self.extrapolate == "clamp":
            return self.y[0]
        return self.y[0] + self._left_slope() * (t - self.x[0])

    def _extrapolate_right(self, t):
        if self.extrapolate == "error":
            raise ValueError(
                "查询点 %r 大于最大横坐标 %r，且 extrapolate='error'"
                % (t, self.x[-1])
            )
        if self.extrapolate == "clamp":
            return self.y[-1]
        return self.y[-1] + self._right_slope() * (t - self.x[-1])

    # 子类需实现
    def _eval_segment(self, k, t):
        raise NotImplementedError

    def _left_slope(self):
        raise NotImplementedError

    def _right_slope(self):
        raise NotImplementedError


class LinearInterpolator(_BaseInterpolator):
    """分段线性插值。"""

    def _eval_segment(self, k, t):
        x0, x1 = self.x[k], self.x[k + 1]
        y0, y1 = self.y[k], self.y[k + 1]
        u = (t - x0) / (x1 - x0)
        return y0 + u * (y1 - y0)

    def _left_slope(self):
        if self.n < 2:
            return 0.0
        return (self.y[1] - self.y[0]) / (self.x[1] - self.x[0])

    def _right_slope(self):
        if self.n < 2:
            return 0.0
        return (self.y[-1] - self.y[-2]) / (self.x[-1] - self.x[-2])


class PchipInterpolator(_BaseInterpolator):
    """保形分段三次 Hermite 插值（Fritsch-Carlson / PCHIP）。

    单点输入退化为常数；两点输入退化为直线。
    """

    def __init__(self, x, y, extrapolate="error", on_duplicate="error"):
        super().__init__(x, y, extrapolate=extrapolate, on_duplicate=on_duplicate)
        self._m = self._compute_slopes()

    def _compute_slopes(self):
        n = self.n
        x, y = self.x, self.y
        if n < 2:
            return [0.0] * n
        h = [x[k + 1] - x[k] for k in range(n - 1)]
        d = [(y[k + 1] - y[k]) / h[k] for k in range(n - 1)]
        m = [0.0] * n
        if n == 2:
            m[0] = m[1] = d[0]
            return m
        m[0] = _pchip_endpoint_slope(h[0], h[1], d[0], d[1])
        m[n - 1] = _pchip_endpoint_slope(h[n - 2], h[n - 3], d[n - 2], d[n - 3])
        for k in range(1, n - 1):
            if d[k - 1] == 0.0 or d[k] == 0.0 or (d[k - 1] < 0.0) != (d[k] < 0.0):
                m[k] = 0.0
            else:
                w1 = 2.0 * h[k] + h[k - 1]
                w2 = h[k] + 2.0 * h[k - 1]
                m[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k])
        return m

    def _eval_segment(self, k, t):
        x0, x1 = self.x[k], self.x[k + 1]
        y0, y1 = self.y[k], self.y[k + 1]
        m0, m1 = self._m[k], self._m[k + 1]
        h = x1 - x0
        u = (t - x0) / h
        u2 = u * u
        u3 = u2 * u
        h00 = 2.0 * u3 - 3.0 * u2 + 1.0
        h10 = u3 - 2.0 * u2 + u
        h01 = -2.0 * u3 + 3.0 * u2
        h11 = u3 - u2
        return h00 * y0 + h10 * h * m0 + h01 * y1 + h11 * h * m1

    def _left_slope(self):
        return self._m[0]

    def _right_slope(self):
        return self._m[-1]


def _pchip_endpoint_slope(h0, h1, d0, d1):
    """PCHIP 端点斜率（非中心三点公式 + 保形截断）。"""
    m = ((2.0 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
    if (m < 0.0) != (d0 < 0.0):
        m = 0.0
    elif (d0 < 0.0) != (d1 < 0.0) and abs(m) > 3.0 * abs(d0):
        m = 3.0 * d0
    return m


def make_interpolator(kind, x, y, extrapolate="error", on_duplicate="error"):
    """便捷工厂：kind 为 'linear' 或 'pchip'。"""
    if kind == "linear":
        return LinearInterpolator(x, y, extrapolate=extrapolate, on_duplicate=on_duplicate)
    if kind == "pchip":
        return PchipInterpolator(x, y, extrapolate=extrapolate, on_duplicate=on_duplicate)
    raise ValueError("未知插值类型: %r（可选 'linear' / 'pchip'）" % (kind,))
