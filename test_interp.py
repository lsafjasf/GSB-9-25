"""interp 库的性质断言测试与边界用例集（仅标准库 unittest）。

运行：python3 test_interp.py   或   python3 -m unittest test_interp -v
"""

import math
import random
import unittest

from interp import LinearInterpolator, PchipInterpolator, make_interpolator


def dense_eval(f, x0, x1, n=257):
    """在 [x0, x1] 上均匀取 n 个点求值。"""
    if n == 1:
        return [f(x0)]
    step = (x1 - x0) / (n - 1)
    out = []
    for i in range(n):
        t = x0 + i * step
        # 钳制浮点末端误差，避免误触发外推
        t = min(max(t, x0), x1)
        out.append(f(t))
    return out


def random_sorted_x(rng, n, lo=-100.0, hi=100.0):
    """生成严格递增、非等距的横坐标（随机间距）。"""
    gaps = [rng.random() + 1e-3 for _ in range(n - 1)]
    total = sum(gaps)
    scale = (hi - lo) / total if total else 1.0
    xs = [lo]
    for g in gaps:
        xs.append(xs[-1] + g * scale)
    return xs


class TestPropertiesRandom(unittest.TestCase):
    """随机数据性质断言：保形性（无过冲）与单调性保持。"""

    def setUp(self):
        self.rng = random.Random(20260925)

    def test_pchip_no_overshoot_random(self):
        """每个区间内的值不超出相邻两采样点的范围（过冲量为零）。"""
        for trial in range(200):
            n = self.rng.randint(2, 30)
            xs = random_sorted_x(self.rng, n)
            ys = [self.rng.uniform(-50, 50) for _ in range(n)]
            f = PchipInterpolator(xs, ys)
            for k in range(n - 1):
                lo, hi = min(ys[k], ys[k + 1]), max(ys[k], ys[k + 1])
                for v in dense_eval(f, xs[k], xs[k + 1], 33):
                    self.assertGreaterEqual(
                        v, lo - 1e-9, "trial %d 区间 %d 下冲: %r < %r" % (trial, k, v, lo))
                    self.assertLessEqual(
                        v, hi + 1e-9, "trial %d 区间 %d 过冲: %r > %r" % (trial, k, v, hi))

    def test_linear_no_overshoot_random(self):
        """线性插值同样不得超出相邻点范围（对拍基准）。"""
        for trial in range(200):
            n = self.rng.randint(2, 30)
            xs = random_sorted_x(self.rng, n)
            ys = [self.rng.uniform(-50, 50) for _ in range(n)]
            f = LinearInterpolator(xs, ys)
            for k in range(n - 1):
                lo, hi = min(ys[k], ys[k + 1]), max(ys[k], ys[k + 1])
                for v in dense_eval(f, xs[k], xs[k + 1], 33):
                    self.assertGreaterEqual(v, lo - 1e-9)
                    self.assertLessEqual(v, hi + 1e-9)

    def test_pchip_monotonicity_random(self):
        """y 单调（非降）时，PCHIP 结果在任意加密网格上同样单调非降。"""
        for trial in range(200):
            n = self.rng.randint(2, 30)
            xs = random_sorted_x(self.rng, n)
            ys = sorted(self.rng.uniform(-50, 50) for _ in range(n))
            f = PchipInterpolator(xs, ys)
            vals = dense_eval(f, xs[0], xs[-1], 501)
            for a, b in zip(vals, vals[1:]):
                self.assertLessEqual(
                    a, b + 1e-9, "trial %d 出现反向变化: %r > %r" % (trial, a, b))

    def test_pchip_monotonicity_decreasing_random(self):
        """y 单调递减时同样保持方向。"""
        for trial in range(100):
            n = self.rng.randint(2, 30)
            xs = random_sorted_x(self.rng, n)
            ys = sorted((self.rng.uniform(-50, 50) for _ in range(n)), reverse=True)
            f = PchipInterpolator(xs, ys)
            vals = dense_eval(f, xs[0], xs[-1], 501)
            for a, b in zip(vals, vals[1:]):
                self.assertGreaterEqual(a, b - 1e-9)

    def test_pchip_matches_linear_sign_per_segment(self):
        """与线性插值对拍：每个单调区间上不得出现反向变化。

        若某区间 y_k <= y_{k+1}，则该区间内 PCHIP 与线性都必须非降。
        """
        for trial in range(200):
            n = self.rng.randint(2, 30)
            xs = random_sorted_x(self.rng, n)
            ys = [self.rng.uniform(-50, 50) for _ in range(n)]
            fp = PchipInterpolator(xs, ys)
            fl = LinearInterpolator(xs, ys)
            for k in range(n - 1):
                vp = dense_eval(fp, xs[k], xs[k + 1], 33)
                vl = dense_eval(fl, xs[k], xs[k + 1], 33)
                for a, b in zip(vp, vp[1:]):
                    if ys[k] <= ys[k + 1]:
                        self.assertLessEqual(a, b + 1e-9)
                    else:
                        self.assertGreaterEqual(a, b - 1e-9)
                for a, b in zip(vl, vl[1:]):
                    if ys[k] <= ys[k + 1]:
                        self.assertLessEqual(a, b + 1e-12)
                    else:
                        self.assertGreaterEqual(a, b - 1e-12)

    def test_pchip_passes_through_nodes(self):
        """插值而非逼近：必须精确经过采样点。"""
        for _ in range(100):
            n = self.rng.randint(1, 20)
            xs = random_sorted_x(self.rng, n)
            ys = [self.rng.uniform(-50, 50) for _ in range(n)]
            for f in (PchipInterpolator(xs, ys), LinearInterpolator(xs, ys)):
                for xi, yi in zip(f.x, f.y):
                    self.assertAlmostEqual(f(xi), yi, places=9)

    def test_nonuniform_spacing_not_degenerate(self):
        """非等距采样：结果仍落在全局值域内，且经过所有节点。"""
        for _ in range(100):
            n = self.rng.randint(3, 25)
            # 极端非等距：间距跨多个数量级
            xs = [0.0]
            for _ in range(n - 1):
                xs.append(xs[-1] + 10.0 ** self.rng.uniform(-4, 2))
            ys = [self.rng.uniform(-10, 10) for _ in range(n)]
            f = PchipInterpolator(xs, ys)
            glo, ghi = min(ys), max(ys)
            vals = dense_eval(f, xs[0], xs[-1], 1001)
            for v in vals:
                self.assertGreaterEqual(v, glo - 1e-9)
                self.assertLessEqual(v, ghi + 1e-9)
            for xi, yi in zip(xs, ys):
                self.assertAlmostEqual(f(xi), yi, places=8)


class TestDuplicates(unittest.TestCase):
    """横坐标重复的处理规则：默认报错；可选取最后一个。"""

    def test_duplicate_raises_by_default(self):
        for cls in (LinearInterpolator, PchipInterpolator):
            with self.assertRaises(ValueError):
                cls([0.0, 1.0, 1.0, 2.0], [0.0, 1.0, 9.0, 4.0])

    def test_duplicate_last_wins(self):
        for cls in (LinearInterpolator, PchipInterpolator):
            f = cls([0.0, 1.0, 1.0, 2.0], [0.0, 1.0, 9.0, 4.0], on_duplicate="last")
            self.assertEqual(f.x, [0.0, 1.0, 2.0])
            self.assertEqual(f.y, [0.0, 9.0, 4.0])  # x=1 处取最后的 y=9
            self.assertAlmostEqual(f(1.0), 9.0)

    def test_duplicate_unsorted_input(self):
        """未排序输入先去重规则同样生效（按 x 排序后取最后）。"""
        f = PchipInterpolator([2.0, 1.0, 0.0, 1.0], [4.0, 1.0, 0.0, 9.0],
                              on_duplicate="last")
        self.assertEqual(f.x, [0.0, 1.0, 2.0])
        self.assertEqual(f.y, [0.0, 9.0, 4.0])

    def test_bad_duplicate_mode(self):
        with self.assertRaises(ValueError):
            LinearInterpolator([0, 1], [0, 1], on_duplicate="first")


class TestExtrapolation(unittest.TestCase):
    """外推策略：默认拒绝；可选保持端点 / 线性外推。"""

    def setUp(self):
        self.xs = [0.0, 1.0, 2.0]
        self.ys = [0.0, 2.0, 2.0]

    def test_default_is_error(self):
        for cls in (LinearInterpolator, PchipInterpolator):
            f = cls(self.xs, self.ys)
            self.assertEqual(f.extrapolate, "error")
            with self.assertRaises(ValueError):
                f(-1e-9)
            with self.assertRaises(ValueError):
                f(2.0 + 1e-9)
            # 端点本身合法
            self.assertAlmostEqual(f(0.0), 0.0)
            self.assertAlmostEqual(f(2.0), 2.0)

    def test_clamp_holds_endpoints(self):
        for cls in (LinearInterpolator, PchipInterpolator):
            f = cls(self.xs, self.ys, extrapolate="clamp")
            self.assertEqual(f(-1e6), 0.0)
            self.assertEqual(f(1e6), 2.0)

    def test_linear_extrapolation_values(self):
        fl = LinearInterpolator(self.xs, self.ys, extrapolate="linear")
        # 左端斜率 = (2-0)/(1-0) = 2；右端斜率 = 0
        self.assertAlmostEqual(fl(-0.5), -1.0)
        self.assertAlmostEqual(fl(3.0), 2.0)
        fp = PchipInterpolator([0.0, 1.0], [0.0, 2.0], extrapolate="linear")
        self.assertAlmostEqual(fp(-1.0), -2.0)  # 两点退化为直线
        self.assertAlmostEqual(fp(2.0), 4.0)

    def test_linear_extrapolation_extreme_extension(self):
        """极端延伸：linear 无界发散，clamp 有界，error 拒绝。"""
        fl = LinearInterpolator([0.0, 1.0], [0.0, 1.0], extrapolate="linear")
        fc = LinearInterpolator([0.0, 1.0], [0.0, 1.0], extrapolate="clamp")
        fe = LinearInterpolator([0.0, 1.0], [0.0, 1.0])  # 默认 error
        self.assertEqual(fl(1e12), 1e12)          # 发散
        self.assertEqual(fc(1e12), 1.0)           # 有界
        with self.assertRaises(ValueError):
            fe(1e12)

    def test_bad_extrapolate_mode(self):
        with self.assertRaises(ValueError):
            PchipInterpolator([0, 1], [0, 1], extrapolate="nearest")


class TestEdgeCases(unittest.TestCase):
    """边界用例集：单点、两点、全部相同、极密采样、跨数量级横坐标。"""

    def test_single_point(self):
        for cls in (LinearInterpolator, PchipInterpolator):
            f = cls([3.0], [7.0])
            self.assertEqual(f(3.0), 7.0)
            with self.assertRaises(ValueError):
                f(3.1)
            fc = cls([3.0], [7.0], extrapolate="clamp")
            self.assertEqual(fc(-100.0), 7.0)
            self.assertEqual(fc(100.0), 7.0)
            fl = cls([3.0], [7.0], extrapolate="linear")
            self.assertEqual(fl(100.0), 7.0)  # 单点无斜率，线性外推退化为常数

    def test_two_points_pchip_equals_linear(self):
        xs, ys = [1.0, 4.0], [2.0, 8.0]
        fp = PchipInterpolator(xs, ys)
        fl = LinearInterpolator(xs, ys)
        for i in range(101):
            t = 1.0 + 3.0 * i / 100.0
            self.assertAlmostEqual(fp(t), fl(t), places=12)

    def test_all_identical_y(self):
        xs = [0.0, 0.5, 2.0, 10.0]
        ys = [5.0] * 4
        for cls in (LinearInterpolator, PchipInterpolator):
            f = cls(xs, ys)
            for v in dense_eval(f, 0.0, 10.0, 501):
                self.assertAlmostEqual(v, 5.0, places=12)

    def test_all_identical_x_with_last_wins(self):
        f = PchipInterpolator([2.0, 2.0, 2.0], [1.0, 2.0, 3.0], on_duplicate="last")
        self.assertEqual(f.x, [2.0])
        self.assertEqual(f(2.0), 3.0)

    def test_very_dense_samples(self):
        rng = random.Random(7)
        n = 5000
        xs = sorted(rng.random() for _ in range(n))
        # 保证严格递增
        xs = [i * 1e-6 + xs[i] * 1e-9 for i in range(n)]
        ys = [math.sin(50 * x) for x in xs]
        f = PchipInterpolator(xs, ys)
        vals = dense_eval(f, xs[0], xs[-1], 2001)
        for v in vals:
            self.assertGreaterEqual(v, min(ys) - 1e-9)
            self.assertLessEqual(v, max(ys) + 1e-9)
        self.assertAlmostEqual(f(xs[2500]), ys[2500], places=9)

    def test_x_spanning_orders_of_magnitude(self):
        xs = [1e-9, 1e-6, 1e-3, 1.0, 1e3, 1e6]
        ys = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
        f = PchipInterpolator(xs, ys)
        # 单调性保持
        log_grid = [10.0 ** e for e in
                    [ -9 + 15 * i / 500.0 for i in range(501)]]
        prev = None
        for t in log_grid:
            v = f(t)
            if prev is not None:
                self.assertGreaterEqual(v, prev - 1e-9)
            prev = v
        # 过冲为零
        for k in range(len(xs) - 1):
            lo, hi = ys[k], ys[k + 1]
            for i in range(33):
                t = xs[k] + (xs[k + 1] - xs[k]) * i / 32.0
                v = f(t)
                self.assertGreaterEqual(v, lo - 1e-9)
                self.assertLessEqual(v, hi + 1e-9)

    def test_empty_and_mismatched_input(self):
        with self.assertRaises(ValueError):
            LinearInterpolator([], [])
        with self.assertRaises(ValueError):
            PchipInterpolator([0.0, 1.0], [0.0])
        with self.assertRaises(ValueError):
            LinearInterpolator([0.0, float("nan")], [0.0, 1.0])

    def test_factory(self):
        f = make_interpolator("pchip", [0, 1], [0, 1])
        self.assertIsInstance(f, PchipInterpolator)
        f = make_interpolator("linear", [0, 1], [0, 1])
        self.assertIsInstance(f, LinearInterpolator)
        with self.assertRaises(ValueError):
            make_interpolator("cubic", [0, 1], [0, 1])

    def test_batch_eval(self):
        f = PchipInterpolator([0.0, 1.0, 2.0], [0.0, 1.0, 0.0])
        out = f([0.0, 0.5, 1.0])
        self.assertEqual(len(out), 3)
        self.assertAlmostEqual(out[2], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
