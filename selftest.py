"""selftest.py -- matrix_factor 库的自测、病态判定用例与性能基准。

运行：python3 selftest.py
"""

import math
import random
import time

import matrix_factor as mf


# ------------------------------------------------------------ 构造工具

def random_matrix(m, n, rng, lo=-1.0, hi=1.0):
    return [[rng.uniform(lo, hi) for _ in range(n)] for _ in range(m)]


def random_well_conditioned(n, kappa, rng):
    """A = Q1 D Q2，Q1/Q2 为随机正交阵，D 的对角在 [1/kappa, 1] 几何分布，
    因此 cond_2(A) ~= kappa。"""
    Q1, _ = mf.qr_factor(random_matrix(n, n, rng))
    Q2, _ = mf.qr_factor(random_matrix(n, n, rng))
    d = [kappa ** (-i / (n - 1)) if n > 1 else 1.0 for i in range(n)]
    return [[sum(Q1[i][k] * d[k] * Q2[j][k] for k in range(n))
             for j in range(n)] for i in range(n)]


def hilbert(n):
    return [[1.0 / (i + j + 1) for j in range(n)] for i in range(n)]


# ------------------------------------------------------------ 1. 特殊输入

def test_special_inputs():
    print("== 1. 特殊输入覆盖 ==")
    rng = random.Random(1)

    # 单位矩阵
    n = 6
    I = [[float(i == j) for j in range(n)] for i in range(n)]
    b = [rng.uniform(-1, 1) for _ in range(n)]
    r = mf.solve(I, b)
    err = max(abs(r.x[i] - b[i]) for i in range(n))
    assert r.status == "ok" and err == 0.0
    print(f"  单位矩阵      : status={r.status} rcond={r.rcond:.2e} "
          f"relres={r.relres:.2e} max|x-b|={err:.1e}")

    # 零矩阵 -> 精确奇异
    Z = [[0.0] * 4 for _ in range(4)]
    r = mf.solve(Z, [1.0, 2.0, 3.0, 4.0])
    assert r.status == "singular" and r.x is None and r.rcond == 0.0
    print(f"  零矩阵        : status={r.status} rcond={r.rcond:.1f} "
          f"(精确奇异, 拒绝求解)")

    # 尺度差异跨越 8 个数量级（对角缩放后仍良态）
    n = 30
    M = random_well_conditioned(n, 1e2, rng)
    d = [10.0 ** rng.uniform(-4, 4) for _ in range(n)]
    A = [[d[i] * M[i][j] for j in range(n)] for i in range(n)]  # 行缩放
    xt = [rng.uniform(-1, 1) for _ in range(n)]
    b = mf.matvec(A, xt)
    r = mf.solve(A, b)
    relerr = max(abs(r.x[i] - xt[i]) for i in range(n)) / max(abs(t) for t in xt)
    assert r.status == "ok" and r.relres < 1e-8
    print(f"  尺度跨 1e8    : status={r.status} rcond={r.rcond:.2e} "
          f"relres={r.relres:.2e} 解相对误差={relerr:.2e}")

    # 需要行交换：主对角线全为 0，无主元消元必失败
    A = [[0.0, 2.0, 1.0],
         [1.0, 0.0, 3.0],
         [4.0, 1.0, 0.0]]
    xt = [1.0, -2.0, 3.0]
    b = mf.matvec(A, xt)
    r = mf.solve(A, b)
    err = max(abs(r.x[i] - xt[i]) for i in range(3))
    assert r.status == "ok" and err < 1e-12
    print(f"  需行交换      : status={r.status} rcond={r.rcond:.2e} "
          f"relres={r.relres:.2e} max|x-x_true|={err:.1e}")

    # 非方阵：超定 (10x4) 相容系统，最小二乘应还原真解
    A = random_matrix(10, 4, rng)
    xt = [rng.uniform(-1, 1) for _ in range(4)]
    b = mf.matvec(A, xt)
    r = mf.lstsq(A, b)
    err = max(abs(r.x[i] - xt[i]) for i in range(4))
    assert r.status == "ok" and err < 1e-10
    print(f"  非方阵 10x4   : status={r.status} rcond={r.rcond:.2e} "
          f"relres={r.relres:.2e} max|x-x_true|={err:.1e}")

    # 非方阵：超定不相容系统，验证法方程残差 A^T(Ax-b) ~ 0
    b = [rng.uniform(-5, 5) for _ in range(10)]
    r = mf.lstsq(A, b)
    Ax = mf.matvec(A, r.x)
    grad = [sum(A[i][j] * (Ax[i] - b[i]) for i in range(10)) for j in range(4)]
    gnorm = mf.norm2(grad)
    assert r.status == "ok" and gnorm < 1e-9
    print(f"  超定不相容    : status={r.status} ||A^T(Ax-b)||={gnorm:.2e} "
          f"(法方程最优性)")

    # 欠定 (m < n) 明确拒绝
    try:
        mf.lstsq(random_matrix(3, 5, rng), [1.0, 2.0, 3.0])
        raise AssertionError("欠定系统应被拒绝")
    except ValueError as e:
        print(f"  欠定 3x5      : 正确拒绝 ({e})")


# ------------------------------------------------------------ 2. 病态判定

def test_classification():
    print("\n== 2. 病态判定用例（三分类）==")
    rng = random.Random(2)

    # (a) 精确奇异：两行完全相同
    A = random_matrix(8, 8, rng)
    A[7] = A[0][:]
    r = mf.solve(A, [1.0] * 8)
    assert r.status == "singular"
    print(f"  重复行(秩亏)  : status={r.status:14s} rcond={r.rcond:.1f}")

    # (b) 数值病态：Hilbert 矩阵 H12, cond_2 ~ 1.7e16
    A = hilbert(12)
    b = mf.matvec(A, [1.0] * 12)
    r = mf.solve(A, b)
    assert r.status == "ill_conditioned" and r.rcond < 1e-12
    print(f"  Hilbert(12)   : status={r.status:14s} rcond={r.rcond:.2e} "
          f"relres={r.relres:.2e}  <- 病态, 不当成功")

    # (b2) 数值病态：构造 cond=1e14 的矩阵
    A = random_well_conditioned(20, 1e14, rng)
    b = mf.matvec(A, [1.0] * 20)
    r = mf.solve(A, b)
    assert r.status == "ill_conditioned"
    print(f"  cond~1e14     : status={r.status:14s} rcond={r.rcond:.2e}")

    # (c) 正常：cond=1e6 仍应正常求解
    A = random_well_conditioned(20, 1e6, rng)
    xt = [rng.uniform(-1, 1) for _ in range(20)]
    b = mf.matvec(A, xt)
    r = mf.solve(A, b)
    assert r.status == "ok" and r.relres < 1e-8
    print(f"  cond~1e6      : status={r.status:14s} rcond={r.rcond:.2e} "
          f"relres={r.relres:.2e}")

    # strict 模式：病态直接抛异常
    try:
        mf.solve(hilbert(12), [1.0] * 12, strict=True)
        raise AssertionError("strict 模式下病态应抛异常")
    except mf.IllConditionedError as e:
        print(f"  strict 模式   : 病态正确抛出 IllConditionedError ({e})")


# ------------------------------------------------------------ 3. 批量验证

def test_batch():
    print("\n== 3. 随机良态矩阵批量验证 ==")
    rng = random.Random(42)
    trials, max_relres, min_rcond, max_relerr = 200, 0.0, 1.0, 0.0
    for t in range(trials):
        n = rng.randint(2, 40)
        kappa = 10.0 ** rng.uniform(0, 6)          # cond 覆盖 1 ~ 1e6
        A = random_well_conditioned(n, kappa, rng)
        xt = [rng.uniform(-1, 1) for _ in range(n)]
        b = mf.matvec(A, xt)
        r = mf.solve(A, b)
        assert r.status == "ok", f"trial {t}: 意外状态 {r.status}"
        assert r.relres < 1e-8, f"trial {t}: 残差 {r.relres:.2e} 超阈值"
        relerr = max(abs(r.x[i] - xt[i]) for i in range(n)) / max(
            abs(v) for v in xt)
        max_relres = max(max_relres, r.relres)
        min_rcond = min(min_rcond, r.rcond)
        max_relerr = max(max_relerr, relerr)
    print(f"  {trials} 组 (n=2..40, cond=1..1e6) 全部 status=ok")
    print(f"  最大相对残差 = {max_relres:.2e} (< 阈值 1e-8)")
    print(f"  最小 rcond   = {min_rcond:.2e}")
    print(f"  最大解相对误差 = {max_relerr:.2e} (与 cond*eps 量级一致)")

    # QR 最小二乘批量（非方阵）
    max_relres = 0.0
    for t in range(100):
        m = rng.randint(6, 40)
        n = rng.randint(2, m)
        A = random_matrix(m, n, rng)
        xt = [rng.uniform(-1, 1) for _ in range(n)]
        b = mf.matvec(A, xt)
        r = mf.lstsq(A, b)
        assert r.status == "ok" and r.relres < 1e-8
        max_relres = max(max_relres, r.relres)
    print(f"  100 组随机超定最小二乘全部 ok, 最大相对残差 = {max_relres:.2e}")


# ------------------------------------------------------------ 4. 性能基准

def bench():
    print("\n== 4. 性能数据（纯 Python 标准库, 单次计时）==")
    rng = random.Random(7)
    print(f"  {'n':>5} {'LU分解':>10} {'QR分解':>10} {'LU求解':>10} "
          f"{'QR求解':>10} {'QR/LU分解比':>12}")
    for n in (64, 128, 256, 384):
        A = random_matrix(n, n, rng)
        b = [rng.uniform(-1, 1) for _ in range(n)]

        t0 = time.perf_counter(); LU, piv, _ = mf.lu_factor(A)
        t_lu = time.perf_counter() - t0
        t0 = time.perf_counter(); Q, R = mf.qr_factor(A)
        t_qr = time.perf_counter() - t0
        t0 = time.perf_counter(); mf.lu_solve(LU, piv, b)
        t_ls = time.perf_counter() - t0
        t0 = time.perf_counter(); mf.qr_solve(Q, R, b)
        t_qs = time.perf_counter() - t0

        print(f"  {n:>5} {t_lu*1e3:>9.1f}ms {t_qr*1e3:>9.1f}ms "
              f"{t_ls*1e3:>9.2f}ms {t_qs*1e3:>9.2f}ms {t_qr/t_lu:>11.2f}x")


if __name__ == "__main__":
    test_special_inputs()
    test_classification()
    test_batch()
    bench()
    print("\n全部断言通过。")
