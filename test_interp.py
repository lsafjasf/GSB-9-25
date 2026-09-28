"""interp 库自测：性质断言 + 过冲对照 + 边界用例。仅标准库。

运行：python3 test_interp.py [-v]
"""
import math
import random
import unittest

from interp import (
    Interpolator,
    Kernel,
    LinearInterpolator,
    MonotoneCubicInterpolator,
    available_kernels,
    register_kernel,
    upsample,
)


# ---------------------------------------------------------------- 工具

def dense_grid(x0, x1, n):
    return [x0 + (x1 - x0) * k / (n - 1) for k in range(n)]


def interval_overshoot(f, xs, ys, samples_per_interval=50):
    """逐区间统计过冲量：插值值越出相邻两端采样点范围的最大距离。

    过冲量 = max(0, lo - v, v - hi) 在所有区间、所有密采样点上的最大值。
    对保形插值该值必须严格为 0（浮点误差内）。
    """
    worst = 0.0
    for i in range(len(xs) - 1):
        lo, hi = min(ys[i], ys[i + 1]), max(ys[i], ys[i + 1])
        for x in dense_grid(xs[i], xs[i + 1], samples_per_interval):
            v = f(x)
            worst = max(worst, lo - v, v - hi)
    return worst


def max_reversal(f, xs, samples_per_interval=50):
    """单调区间上的最大反向变化量：相邻密采样点间负增量的最大值。"""
    worst = 0.0
    prev = None
    for i in range(len(xs) - 1):
        for x in dense_grid(xs[i], xs[i + 1], samples_per_interval):
            v = f(x)
            if prev is not None:
                worst = max(worst, prev - v)
            prev = v
    return worst


class CatmullRom:
    """未限幅的普通三次样条（Catmull-Rom），仅用于过冲对照。"""

    def __init__(self, xs, ys):
        self.xs, self.ys = xs, ys

    def __call__(self, x):
        xs, ys = self.xs, self.ys
        n = len(xs)
        import bisect
        i = max(0, min(n - 2, bisect.bisect_right(xs, x) - 1))
        x0, x1 = xs[i], xs[i + 1]
        y0, y1 = ys[i], ys[i + 1]
        m0 = (ys[i + 1] - ys[i - 1]) / (xs[i + 1] - xs[i - 1]) if i > 0 \
            else (ys[1] - ys[0]) / (xs[1] - xs[0])
        m1 = (ys[i + 2] - ys[i]) / (xs[i + 2] - xs[i]) if i + 2 < n \
            else (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
        h = x1 - x0
        t = (x - x0) / h
        t2, t3 = t * t, t * t * t
        return ((2 * t3 - 3 * t2 + 1) * y0 + (t3 - 2 * t2 + t) * h * m0
                + (-2 * t3 + 3 * t2) * y1 + (t3 - t2) * h * m1)


class NearestKernel(Kernel):
    """测试用自定义核：最近邻（取区间左端点值，中点右取右端点值）。"""

    name = "nearest_test"

    def build(self, xs, ys):
        self.xs, self.ys = xs, ys

    def eval_segment(self, i, x):
        xs, ys = self.xs, self.ys
        return ys[i] if x - xs[i] < xs[i + 1] - x else ys[i + 1]


# ---------------------------------------------------------------- 核可插拔

class TestPluggableKernel(unittest.TestCase):
    def test_builtin_names_and_aliases(self):
        self.assertIn("linear", available_kernels())
        self.assertIn("pchip", available_kernels())
        xs, ys = [0, 1, 2], [0.0, 2.0, 3.0]
        by_name = Interpolator(xs, ys, kernel="linear")
        by_alias = Interpolator(xs, ys, kernel="monotone_cubic")
        self.assertEqual(by_name(0.5), 1.0)
        # 默认核与旧类都是 pchip
        self.assertAlmostEqual(Interpolator(xs, ys)(0.5),
                               MonotoneCubicInterpolator(xs, ys)(0.5))
        self.assertAlmostEqual(by_alias(0.5),
                               Interpolator(xs, ys, kernel="pchip")(0.5))

    def test_kernel_as_class_and_instance(self):
        xs, ys = [0.0, 1.0, 2.0], [10.0, 20.0, 30.0]
        f_cls = Interpolator(xs, ys, kernel=NearestKernel)
        k = NearestKernel()
        f_obj = Interpolator(xs, ys, kernel=k)
        self.assertIs(f_obj.kernel, k)
        for f in (f_cls, f_obj):
            self.assertEqual(f(0.1), 10.0)
            self.assertEqual(f(0.6), 20.0)
            self.assertEqual(f(1.5), 30.0)  # 区间[1,2]中点归右端点

    def test_register_kernel_by_name(self):
        register_kernel("nearest", NearestKernel)
        self.assertIn("nearest", available_kernels())
        f = Interpolator([0.0, 1.0], [5.0, 9.0], kernel="nearest")
        self.assertEqual(f.batch([0.1, 0.6, 0.9]), [5.0, 9.0, 9.0])

    def test_custom_kernel_must_implement_eval(self):
        class Bad(Kernel):
            pass
        with self.assertRaises(NotImplementedError):
            Interpolator([0.0, 1.0], [0.0, 1.0], kernel=Bad)(0.5)

    def test_invalid_kernel(self):
        with self.assertRaises(ValueError):
            Interpolator([0, 1], [0, 1], kernel="cubic_spline_xyz")
        with self.assertRaises(TypeError):
            Interpolator([0, 1], [0, 1], kernel=123)
        with self.assertRaises(TypeError):
            Interpolator([0, 1], [0, 1], kernel=object())
        with self.assertRaises(TypeError):
            register_kernel("bad", object)
        with self.assertRaises(ValueError):
            register_kernel("", NearestKernel)


# ---------------------------------------------------------------- 批量上采样

def _make_grid(xs, rng):
    """构造覆盖内点、端点和两侧外推的目标网格（含非等距、跨数量级）。"""
    lo, hi = xs[0], xs[-1]
    grid = []
    grid.extend([lo, hi, (lo + hi) / 2.0])          # 端点 + 中点
    for i in range(len(xs) - 1):                     # 每段随机多点
        a, b = xs[i], xs[i + 1]
        for _ in range(7):
            grid.append(a + (b - a) * rng.random())
    span = hi - lo or 1.0
    grid.extend([lo - 0.5 * span, lo - 1e6 * span,   # 两侧极端外推
                 hi + 0.37 * span, hi + 1e9 * span])
    grid.extend(xs)                                  # 全部原始节点
    rng.shuffle(grid)
    return grid


class TestBatch(unittest.TestCase):
    """批量结果必须与逐点调用逐元素完全相等（同一浮点值，非近似）。"""

    TRIALS = 120

    def _random_samples(self, rng):
        n = rng.randint(2, 25)
        xs, x = [], rng.uniform(-10.0, 10.0)
        for _ in range(n):
            xs.append(x)
            x += rng.uniform(1e-6, 1e3)  # 非等距
        ys = [rng.uniform(-100.0, 100.0) for _ in range(n)]
        return xs, ys

    def test_batch_equals_pointwise_random(self):
        rng = random.Random(20260929)
        kernels = ("linear", "pchip", NearestKernel)
        for _ in range(self.TRIALS):
            xs, ys = self._random_samples(rng)
            grid = _make_grid(xs, rng)
            for kernel in kernels:
                for extrap in ("clamp", "reject", "linear"):
                    f = Interpolator(xs, ys, kernel=kernel, extrapolate=extrap)
                    if extrap == "reject":
                        inner = [x for x in grid if xs[0] <= x <= xs[-1]]
                        batch, pointwise = f.batch(inner), [f(x) for x in inner]
                    else:
                        batch, pointwise = f.batch(grid), [f(x) for x in grid]
                    self.assertEqual(
                        batch, pointwise,
                        f"批量与逐点不一致: kernel={kernel}, extrap={extrap}")
                    # 长度与顺序一致
                    self.assertEqual(len(batch), len(inner if extrap == "reject"
                                                       else grid))

    def test_batch_on_boundary_shapes(self):
        # 单点插值器：批量全部为常数
        one = Interpolator([3.5], [7.0])
        self.assertEqual(one.batch([-1e9, 3.5, 1e9]), [7.0, 7.0, 7.0])
        # 两点、空网格、乱序网格、含重复位置
        f = Interpolator([0.0, 10.0], [0.0, 100.0], kernel="linear")
        self.assertEqual(f.batch([]), [])
        grid = [10.0, -5.0, 3.0, 3.0, 0.0, 20.0]  # clamp
        self.assertEqual(f.batch(grid), [f(x) for x in grid])
        self.assertEqual(f.batch(grid), [100.0, 0.0, 30.0, 30.0, 0.0, 100.0])

    def test_upsample_function_matches(self):
        rng = random.Random(99)
        xs = [0.0, 0.01, 0.5, 5.0]       # 非等距
        ys = [0.0, 2.0, -1.0, 4.0]
        grid = sorted(-0.1 + 5.2 * k / 200 for k in range(201))
        for kernel in ("linear", "pchip", NearestKernel):
            out = upsample(xs, ys, grid, kernel=kernel, extrapolate="linear")
            ref = [Interpolator(xs, ys, kernel=kernel,
                                extrapolate="linear")(x) for x in grid]
            self.assertEqual(out, ref)
            self.assertEqual(len(out), len(grid))
            self.assertTrue(all(math.isfinite(v) for v in out))
        # kwargs 透传：重复横坐标 + last
        out = upsample([0, 1, 1, 2], [0, 1, 9, 4], [0.5, 1.0, 1.5],
                       kernel="linear", duplicates="last")
        self.assertEqual(out, [4.5, 9.0, 6.5])

    def test_batch_invalid_arguments(self):
        f = Interpolator([0.0, 1.0], [0.0, 1.0])
        # 标量 / 字符串不是合法网格
        for bad in (0.5, 3, 3.0, "1.0", None, 1 + 0j):
            with self.assertRaises(TypeError):
                f.batch(bad)
        # 网格含 NaN / inf
        with self.assertRaises(ValueError):
            f.batch([0.5, float("nan")])
        with self.assertRaises(ValueError):
            f.batch([0.5, float("inf")])
        with self.assertRaises(ValueError):
            f.batch([0.5, float("-inf")])
        # 非数值元素
        with self.assertRaises(TypeError):
            f.batch([0.5, "x"])
        with self.assertRaises(TypeError):
            f.batch([0.5, True])
        # 逐点调用仍拒绝非法 x
        with self.assertRaises(ValueError):
            f(float("nan"))
        with self.assertRaises(TypeError):
            f([0.5])  # list 必须走 batch

    def test_upsample_invalid_construction(self):
        with self.assertRaises(ValueError):  # 空采样
            upsample([], [], [0.5])
        with self.assertRaises(ValueError):  # 长度不一致
            upsample([0, 1], [0], [0.5])
        with self.assertRaises(ValueError):  # 重复横坐标默认报错
            upsample([0, 1, 1], [0, 1, 2], [0.5])
        with self.assertRaises(ValueError):  # 越界 reject
            upsample([0, 1], [0, 1], [2.0], extrapolate="reject")
        with self.assertRaises(ValueError):  # 非法外推策略
            upsample([0, 1], [0, 1], [0.5], extrapolate="nan")
        with self.assertRaises(TypeError):  # 网格标量
            upsample([0, 1], [0, 1], 0.5)


# ---------------------------------------------------------------- 性质断言

class TestProperties(unittest.TestCase):
    """随机数据上的两条核心性质：不越界（零过冲）、保持单调。"""

    TRIALS = 200
    TOL = 1e-9

    def _random_case(self, rng, monotone):
        n = rng.randint(2, 30)
        # 非等距横坐标：随机步长，跨度随机（含跨数量级）
        xs, x = [], rng.uniform(-10.0, 10.0)
        for _ in range(n):
            xs.append(x)
            x += rng.uniform(1e-6, 1e3)
        if monotone:
            ys, y = [], rng.uniform(-100.0, 100.0)
            for _ in range(n):
                ys.append(y)
                y += rng.uniform(0.0, 50.0)  # 非降
        else:
            ys = [rng.uniform(-100.0, 100.0) for _ in range(n)]
        return xs, ys

    def test_no_overshoot_random(self):
        rng = random.Random(20260925)
        for _ in range(self.TRIALS):
            xs, ys = self._random_case(rng, monotone=False)
            for cls in (LinearInterpolator, MonotoneCubicInterpolator):
                f = cls(xs, ys)
                over = interval_overshoot(f, f.xs, f.ys)
                self.assertLessEqual(over, self.TOL,
                                     f"{cls.__name__} 过冲 {over}，xs={xs}, ys={ys}")

    def test_monotonicity_random(self):
        rng = random.Random(20260926)
        for _ in range(self.TRIALS):
            xs, ys = self._random_case(rng, monotone=True)
            for cls in (LinearInterpolator, MonotoneCubicInterpolator):
                f = cls(xs, ys)
                rev = max_reversal(f, f.xs)
                self.assertLessEqual(rev, self.TOL,
                                     f"{cls.__name__} 反向变化 {rev}，xs={xs}, ys={ys}")

    def test_nonuniform_spacing_not_degenerate(self):
        # 非等距：密区间 + 疏区间混合，插值仍精确通过采样点且零过冲
        xs = [0.0, 0.1, 0.2, 0.3, 5.0, 50.0, 500.0]
        ys = [0.0, 1.0, 0.5, 2.0, 2.0, -1.0, 3.0]
        for cls in (LinearInterpolator, MonotoneCubicInterpolator):
            f = cls(xs, ys)
            for x, y in zip(xs, ys):
                self.assertAlmostEqual(f(x), y, places=9)
            self.assertLessEqual(interval_overshoot(f, xs, ys), self.TOL)


# ---------------------------------------------------------------- 重复横坐标

class TestDuplicates(unittest.TestCase):
    def test_default_error(self):
        with self.assertRaises(ValueError):
            LinearInterpolator([0, 1, 1, 2], [0, 1, 9, 4])
        with self.assertRaises(ValueError):
            MonotoneCubicInterpolator([0, 1, 1, 2], [0, 1, 9, 4])

    def test_keep_last(self):
        f = LinearInterpolator([0, 1, 1, 2], [0, 1, 9, 4], duplicates="last")
        self.assertEqual(f.xs, [0, 1, 2])
        self.assertEqual(f.ys, [0, 9, 4])  # x=1 取最后的 y=9
        self.assertEqual(f(1), 9)

    def test_keep_first(self):
        f = MonotoneCubicInterpolator([0, 1, 1, 2], [0, 1, 9, 4], duplicates="first")
        self.assertEqual(f.ys, [0, 1, 4])  # x=1 取最先的 y=1

    def test_unsorted_input_sorted(self):
        f = LinearInterpolator([2, 0, 1], [4, 0, 1])
        self.assertEqual(f.xs, [0, 1, 2])
        self.assertEqual(f(0.5), 0.5)

    def test_invalid_mode(self):
        with self.assertRaises(ValueError):
            LinearInterpolator([0, 1], [0, 1], duplicates="merge")


# ---------------------------------------------------------------- 外推策略

class TestExtrapolation(unittest.TestCase):
    XS = [0.0, 1.0, 2.0]
    YS = [0.0, 2.0, 3.0]
    FAR = 1e12  # 极端延伸

    def test_default_is_clamp(self):
        f = LinearInterpolator(self.XS, self.YS)
        self.assertEqual(f(-self.FAR), 0.0)
        self.assertEqual(f(self.FAR), 3.0)

    def test_reject(self):
        for cls in (LinearInterpolator, MonotoneCubicInterpolator):
            f = cls(self.XS, self.YS, extrapolate="reject")
            with self.assertRaises(ValueError):
                f(-1e-9)
            with self.assertRaises(ValueError):
                f(2.0 + 1e-9)
            f(1.0)  # 范围内正常

    def test_clamp_stays_bounded(self):
        for cls in (LinearInterpolator, MonotoneCubicInterpolator):
            f = cls(self.XS, self.YS, extrapolate="clamp")
            self.assertEqual(f(-self.FAR), 0.0)
            self.assertEqual(f(self.FAR), 3.0)

    def test_linear_extends_with_endpoint_slope(self):
        # 线性插值端点斜率 = 端点割线斜率
        f = LinearInterpolator(self.XS, self.YS, extrapolate="linear")
        self.assertAlmostEqual(f(-1.0), -2.0)          # 斜率 2
        self.assertAlmostEqual(f(3.0), 4.0)            # 斜率 1
        self.assertAlmostEqual(f(self.FAR), 3.0 + (self.FAR - 2.0))
        # 保形三次用端点切线斜率外推
        g = MonotoneCubicInterpolator(self.XS, self.YS, extrapolate="linear")
        self.assertAlmostEqual(g(3.0), 3.0 + g._slope_hi())
        self.assertAlmostEqual(g(-1.0), 0.0 - g._slope_lo())
        # 极端延伸下线性外推无界（与 clamp 对照）
        self.assertGreater(abs(g(self.FAR)), 1e6)


# ---------------------------------------------------------------- 过冲对照

class TestOvershootComparison(unittest.TestCase):
    """阶跃/锯齿数据上：线性、保形三次过冲为 0，普通三次样条显著过冲。"""

    CASES = {
        "阶跃": ([0, 1, 2, 3, 4, 5], [0, 0, 0, 1, 1, 1]),
        "锯齿": ([0, 1, 2, 3, 4, 5], [0, 1, 0, 1, 0, 1]),
        "单调台阶": ([0, 1, 2, 3, 4], [0, 0, 1, 1, 2]),
        "非等距阶跃": ([0, 0.01, 0.5, 0.51, 10], [0, 0, 0, 1, 1]),
    }

    def test_overshoot_table(self):
        rows = []
        for name, (xs, ys) in self.CASES.items():
            lin = interval_overshoot(LinearInterpolator(xs, ys), xs, ys)
            mon = interval_overshoot(MonotoneCubicInterpolator(xs, ys), xs, ys)
            cr = interval_overshoot(CatmullRom(xs, ys), xs, ys)
            rows.append((name, lin, mon, cr))
            self.assertEqual(lin, 0.0, f"线性插值过冲: {name}")
            self.assertLessEqual(mon, 1e-9, f"保形三次过冲: {name}")
        # 普通三次在阶跃上必须确实过冲，否则对照无意义
        self.assertGreater(rows[0][3], 0.05)
        print("\n过冲量对照（越出相邻采样点范围的最大距离，0 = 无过冲）")
        print(f"{'用例':<12}{'线性':>12}{'保形三次':>12}{'普通三次':>12}")
        for name, lin, mon, cr in rows:
            print(f"{name:<12}{lin:>12.3e}{mon:>12.3e}{cr:>12.3e}")

    def test_no_reversal_on_monotone(self):
        xs = [0, 1, 2, 3, 4, 5]
        ys = [0, 0, 1, 1, 2, 2]  # 非降台阶
        lin = LinearInterpolator(xs, ys)
        mon = MonotoneCubicInterpolator(xs, ys)
        self.assertEqual(max_reversal(lin, xs), 0.0)
        self.assertLessEqual(max_reversal(mon, xs), 1e-9)
        # 普通三次在单调台阶上出现反向变化（对照）
        self.assertGreater(max_reversal(CatmullRom(xs, ys), xs), 1e-3)


# ---------------------------------------------------------------- 边界用例

class TestEdgeCases(unittest.TestCase):
    def test_single_point(self):
        for cls in (LinearInterpolator, MonotoneCubicInterpolator):
            for extrap in ("reject", "clamp", "linear"):
                f = cls([3.5], [7.0], extrapolate=extrap)
                self.assertEqual(f(3.5), 7.0)
                self.assertEqual(f(-1e9), 7.0)   # 单点 = 常数函数，任何策略同值
                self.assertEqual(f(1e9), 7.0)

    def test_two_points(self):
        lin = LinearInterpolator([0, 10], [0, 100])
        self.assertAlmostEqual(lin(3), 30.0)
        mon = MonotoneCubicInterpolator([0, 10], [0, 100])
        self.assertAlmostEqual(mon(3), 30.0)  # 两点时退化为直线
        self.assertAlmostEqual(mon(10), 100.0)

    def test_all_equal(self):
        xs = [0, 1, 2, 3]
        ys = [5.0] * 4
        for cls in (LinearInterpolator, MonotoneCubicInterpolator):
            f = cls(xs, ys)
            for x in dense_grid(0, 3, 100):
                self.assertAlmostEqual(f(x), 5.0, places=12)
            # 线性外推斜率为 0，不发散
            g = cls(xs, ys, extrapolate="linear")
            self.assertAlmostEqual(g(1e12), 5.0, places=6)

    def test_very_dense_samples(self):
        rng = random.Random(7)
        n = 5000
        xs = sorted(rng.random() for _ in range(n))
        ys = [rng.uniform(-1, 1) for _ in range(n)]
        for cls in (LinearInterpolator, MonotoneCubicInterpolator):
            f = cls(xs, ys, duplicates="last")
            self.assertLessEqual(interval_overshoot(f, f.xs, f.ys, 5), 1e-9)
            for x, y in zip(f.xs[::500], f.ys[::500]):
                self.assertAlmostEqual(f(x), y, places=9)

    def test_magnitude_spanning_x(self):
        # 横坐标跨 12 个数量级
        xs = [1e-9, 1e-6, 1e-3, 1.0, 1e3]
        ys = [0.0, 1.0, 0.0, 1.0, 0.0]
        for cls in (LinearInterpolator, MonotoneCubicInterpolator):
            f = cls(xs, ys)
            self.assertLessEqual(interval_overshoot(f, xs, ys), 1e-9)
            self.assertAlmostEqual(f(1e-3), 0.0)
            self.assertAlmostEqual(f(1e3), 0.0)
            mid = f(5e-4)  # 区间内值仍在 [0,1]
            self.assertGreaterEqual(mid, 0.0)
            self.assertLessEqual(mid, 1.0)

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            LinearInterpolator([], [])
        with self.assertRaises(ValueError):
            LinearInterpolator([0, 1], [0])
        with self.assertRaises(ValueError):
            LinearInterpolator([0, float("nan")], [0, 1])
        with self.assertRaises(ValueError):
            LinearInterpolator([0, 1], [0, 1], extrapolate="cubic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
