"""可插拔核函数：内置线性核、保形三次核（PCHIP），以及自定义核基类。

一个"核"只负责两件事：
  1. build：给定全部升序去重后的采样点，预计算系数；
  2. eval_segment：在第 i 个区间 [x_i, x_{i+1}] 上的区间内插值；
  3. slope：端点处的切线斜率（仅外推策略 extrapolate="linear" 时使用）。

区间外（外推）、重复横坐标、排序等由 Interpolator 统一处理，
因此自定义核无需关心这些逻辑。
"""
from __future__ import annotations


class Kernel:
    """自定义核基类。子类至少实现 eval_segment；按需覆盖 build / slope。

    约定
    ----
    - build(xs, ys) 在构造插值器时调用一次，xs/ys 已升序、无重复、
      且保证 len(xs) >= 2；需要预计算系数时把状态存到 self 上。
    - eval_segment(i, x) 返回第 i 个区间上 x 处的值；调用方保证
      xs[i] <= x <= xs[i+1]。
    - slope_lo() / slope_hi() 返回左右端点切线斜率，用于线性外推；
      默认返回 0.0（即退化为夹取）。
    """

    name = "custom"

    def build(self, xs, ys):
        pass

    def eval_segment(self, i, x):
        raise NotImplementedError("自定义核必须实现 eval_segment(i, x)")

    def slope_lo(self):
        return 0.0

    def slope_hi(self):
        return 0.0


class LinearKernel(Kernel):
    """分段线性核。天然不越出相邻采样点范围、保持单调性。"""

    name = "linear"

    def build(self, xs, ys):
        self.xs = xs
        self.ys = ys

    def eval_segment(self, i, x):
        xs, ys = self.xs, self.ys
        x0, x1 = xs[i], xs[i + 1]
        y0, y1 = ys[i], ys[i + 1]
        t = (x - x0) / (x1 - x0)
        return y0 + t * (y1 - y0)

    def slope_lo(self):
        xs, ys = self.xs, self.ys
        return (ys[1] - ys[0]) / (xs[1] - xs[0])

    def slope_hi(self):
        xs, ys = self.xs, self.ys
        return (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])


class MonotoneCubicKernel(Kernel):
    """Fritsch-Carlson 保形分段三次 Hermite 核（PCHIP）。

    每个区间是三次 Hermite 样条，节点斜率按 Fritsch-Carlson 规则选取：
    相邻割线异号（局部极值）时斜率取 0，否则取割线的加权调和平均。
    保证：
      1. 每个区间内的值不越出该区间两端采样点范围（零过冲）；
      2. 采样点单调（非降/非升）时插值结果同样单调。
    """

    name = "pchip"

    def build(self, xs, ys):
        n = len(xs)
        if n < 2:
            self.xs, self.ys, self.m = xs, ys, [0.0]
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
        self.xs, self.ys, self.m = xs, ys, m

    @staticmethod
    def _edge_slope(h0, h1, d0, d1):
        s = ((2.0 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if s * d0 <= 0.0:
            return 0.0
        if d0 * d1 < 0.0 and abs(s) > 3.0 * abs(d0):
            return 3.0 * d0
        return s

    def eval_segment(self, i, x):
        xs, ys, m = self.xs, self.ys, self.m
        x0, x1 = xs[i], xs[i + 1]
        y0, y1 = ys[i], ys[i + 1]
        m0, m1 = m[i], m[i + 1]
        h = x1 - x0
        t = (x - x0) / h
        t2, t3 = t * t, t * t * t
        h00 = 2.0 * t3 - 3.0 * t2 + 1.0
        h10 = t3 - 2.0 * t2 + t
        h01 = -2.0 * t3 + 3.0 * t2
        h11 = t3 - t2
        return h00 * y0 + h10 * h * m0 + h01 * y1 + h11 * h * m1

    def slope_lo(self):
        return self.m[0]

    def slope_hi(self):
        return self.m[-1]
