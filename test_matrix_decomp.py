"""test_matrix_decomp.py — matrix_decomp 自测 (unittest, 仅标准库)。

运行: python3 -m unittest test_matrix_decomp -v
"""

import math
import random
import unittest

import matrix_decomp as md

RESIDUAL_TOL = 1e-10  # 良好条件矩阵的相对残差断言阈值


def make_well_conditioned(n, rng):
    """随机良态矩阵: 随机矩阵 + n*I (对角占优)。"""
    return [[rng.uniform(-1.0, 1.0) + (n if i == j else 0.0)
             for j in range(n)] for i in range(n)]


def hilbert(n):
    return [[1.0 / (i + j + 1) for j in range(n)] for i in range(n)]


class TestBasicSolve(unittest.TestCase):
    def test_identity(self):
        n = 8
        A = [[float(i == j) for j in range(n)] for i in range(n)]
        b = [float(i + 1) for i in range(n)]
        for method in ("lu", "qr"):
            r = md.solve(A, b, method=method)
            self.assertEqual(r.status, md.SolveStatus.OK)
            self.assertLess(r.relative_residual, RESIDUAL_TOL)
            for xi, bi in zip(r.x, b):
                self.assertAlmostEqual(xi, bi, places=12)

    def test_zero_matrix_exact_singular(self):
        A = [[0.0, 0.0], [0.0, 0.0]]
        for method in ("lu", "qr"):
            r = md.solve(A, [1.0, 2.0], method=method)
            self.assertEqual(r.status, md.SolveStatus.EXACT_SINGULAR)
            self.assertIsNone(r.x)
            self.assertEqual(r.condition_estimate, math.inf)

    def test_duplicate_rows_exact_singular(self):
        A = [[1.0, 2.0, 3.0], [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        r = md.solve(A, [1.0, 1.0, 1.0])
        self.assertEqual(r.status, md.SolveStatus.EXACT_SINGULAR)

    def test_row_swap_required(self):
        # 首主元为 0, 必须行交换
        A = [[0.0, 1.0], [1.0, 0.0]]
        b = [3.0, 7.0]
        r = md.solve(A, b)
        self.assertEqual(r.status, md.SolveStatus.OK)
        self.assertAlmostEqual(r.x[0], 7.0, places=12)
        self.assertAlmostEqual(r.x[1], 3.0, places=12)
        self.assertLess(r.relative_residual, RESIDUAL_TOL)

    def test_row_swap_partial_pivoting_stability(self):
        # 经典例子: 不选主元会灾难性舍入, 部分主元稳定
        eps = 1e-20
        A = [[eps, 1.0], [1.0, 1.0]]
        b = [1.0, 2.0]
        r = md.solve(A, b)
        self.assertEqual(r.status, md.SolveStatus.OK)
        self.assertAlmostEqual(r.x[0], 1.0, places=6)
        self.assertAlmostEqual(r.x[1], 1.0, places=6)
        self.assertLess(r.relative_residual, RESIDUAL_TOL)


class TestScaling(unittest.TestCase):
    def test_diagonal_scale_span_1e10(self):
        # 对角元素跨越 1e-5 .. 1e5, 条件数 ~1e10, 仍可解
        d = [1e-5, 1e-2, 1.0, 1e2, 1e5]
        A = [[d[i] if i == j else 0.0 for j in range(5)] for i in range(5)]
        b = [d[i] * (i + 1.0) for i in range(5)]  # 真解 x = [1,2,3,4,5]
        r = md.solve(A, b)
        self.assertEqual(r.status, md.SolveStatus.OK)
        self.assertLess(r.relative_residual, RESIDUAL_TOL)
        for i in range(5):
            self.assertAlmostEqual(r.x[i], i + 1.0, places=8)

    def test_row_scaling_span_1e12(self):
        # 行尺度跨越 12 个数量级, 部分主元 LU 仍应给出小残差
        rng = random.Random(7)
        n = 6
        base = make_well_conditioned(n, rng)
        scales = [10.0 ** (2 * i - 5) for i in range(n)]  # 1e-5 .. 1e5
        A = [[base[i][j] * scales[i] for j in range(n)] for i in range(n)]
        x_true = [rng.uniform(-1, 1) for _ in range(n)]
        b = [sum(A[i][j] * x_true[j] for j in range(n)) for i in range(n)]
        r = md.solve(A, b)
        self.assertEqual(r.status, md.SolveStatus.OK)
        self.assertLess(r.relative_residual, RESIDUAL_TOL)


class TestIllConditioned(unittest.TestCase):
    def test_hilbert_12_is_ill_conditioned(self):
        A = hilbert(12)
        b = [sum(row) for row in A]  # 真解全 1
        r = md.solve(A, b)
        # 病态必须被识别, 不得当作成功
        self.assertEqual(r.status, md.SolveStatus.ILL_CONDITIONED)
        self.assertFalse(r.ok)
        self.assertGreater(r.condition_estimate, md.DEFAULT_COND_THRESHOLD)
        print(f"\n[Hilbert-12] cond_est={r.condition_estimate:.3e} "
              f"rel_resid={r.relative_residual:.3e}")

    def test_nearly_dependent_columns(self):
        # 两列几乎相同: 数值病态但非精确奇异
        A = [[1.0, 1.0], [1.0, 1.0 + 1e-14]]
        r = md.solve(A, [2.0, 2.0])
        self.assertEqual(r.status, md.SolveStatus.ILL_CONDITIONED)
        self.assertGreater(r.condition_estimate, md.DEFAULT_COND_THRESHOLD)

    def test_exact_vs_ill_distinguished(self):
        # 精确奇异与病态必须是不同结论
        exact = md.solve([[1.0, 2.0], [2.0, 4.0]], [3.0, 6.0])
        ill = md.solve([[1.0, 2.0], [2.0, 4.0 + 1e-13]], [3.0, 6.0])
        self.assertEqual(exact.status, md.SolveStatus.EXACT_SINGULAR)
        self.assertEqual(ill.status, md.SolveStatus.ILL_CONDITIONED)


class TestLeastSquares(unittest.TestCase):
    def test_overdetermined_exact_fit(self):
        # 50x3, 真解已知, 右端在列空间内
        rng = random.Random(11)
        m, n = 50, 3
        A = [[rng.uniform(-1, 1) for _ in range(n)] for _ in range(m)]
        x_true = [1.5, -2.0, 0.75]
        b = [sum(A[i][j] * x_true[j] for j in range(n)) for i in range(m)]
        r = md.solve(A, b, method="qr")
        self.assertEqual(r.status, md.SolveStatus.OK)
        self.assertLess(r.relative_residual, RESIDUAL_TOL)
        for xi, ti in zip(r.x, x_true):
            self.assertAlmostEqual(xi, ti, places=10)

    def test_overdetermined_with_noise(self):
        # 含噪声: 验证法方程残差 ||A^T(b-Ax)|| 小
        rng = random.Random(13)
        m, n = 80, 4
        A = [[rng.uniform(-1, 1) for _ in range(n)] for _ in range(m)]
        x_true = [1.0, -1.0, 2.0, 0.5]
        b = [sum(A[i][j] * x_true[j] for j in range(n))
             + rng.uniform(-0.01, 0.01) for i in range(m)]
        r = md.solve(A, b, method="qr")
        self.assertEqual(r.status, md.SolveStatus.OK)
        Ax = md.mat_vec(A, r.x)
        normal_resid = md.norm_2_vec(
            [sum(A[i][j] * (b[i] - Ax[i]) for i in range(m)) for j in range(n)])
        self.assertLess(normal_resid, 1e-8)

    def test_zero_column_exact_singular(self):
        # 含零列 -> R 对角精确为 0 -> 精确奇异
        rng = random.Random(17)
        m = 20
        A = [[rng.uniform(-1, 1), 0.0, rng.uniform(-1, 1)] for _ in range(m)]
        r = md.solve(A, [1.0] * m, method="qr")
        self.assertEqual(r.status, md.SolveStatus.EXACT_SINGULAR)

    def test_rank_deficient_least_squares(self):
        # 第三列 = 前两列之和(浮点表示) -> 数值秩亏, 属于病态而非精确奇异
        rng = random.Random(17)
        m = 20
        c1 = [rng.uniform(-1, 1) for _ in range(m)]
        c2 = [rng.uniform(-1, 1) for _ in range(m)]
        A = [[c1[i], c2[i], c1[i] + c2[i]] for i in range(m)]
        r = md.solve(A, [1.0] * m, method="qr")
        self.assertNotEqual(r.status, md.SolveStatus.OK)
        self.assertGreater(r.condition_estimate, md.DEFAULT_COND_THRESHOLD)


class TestRandomBatch(unittest.TestCase):
    def test_random_well_conditioned_batch(self):
        rng = random.Random(2026)
        worst = {"lu": 0.0, "qr": 0.0}
        cases = 0
        for _ in range(50):
            n = rng.randint(3, 40)
            A = make_well_conditioned(n, rng)
            x_true = [rng.uniform(-10, 10) for _ in range(n)]
            b = [sum(A[i][j] * x_true[j] for j in range(n)) for i in range(n)]
            for method in ("lu", "qr"):
                r = md.solve(A, b, method=method)
                self.assertEqual(r.status, md.SolveStatus.OK,
                                 f"n={n} method={method} 被误判: {r.message}")
                self.assertLess(r.relative_residual, RESIDUAL_TOL,
                                f"n={n} method={method} 残差过大")
                worst[method] = max(worst[method], r.relative_residual)
                cases += 1
        print(f"\n[batch] {cases} 个随机良态用例全部通过; "
              f"最大相对残差 LU={worst['lu']:.2e} QR={worst['qr']:.2e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
