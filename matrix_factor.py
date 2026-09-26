"""matrix_factor.py -- 纯标准库矩阵分解库（Python 3, 无第三方依赖）。

功能
----
  lu_factor / lu_solve   : 带部分主元（行交换）的 LU 三角分解与求解
  qr_factor / qr_solve   : Householder 正交 QR 分解（要求 m >= n）与最小二乘
  solve(A, b)            : 方阵线性方程组求解，带健康度分类
  lstsq(A, b)            : 最小二乘求解（支持非方阵, m >= n），带健康度分类

主元选择规则
------------
LU 采用**部分主元（partial pivoting）**：处理第 k 列时，在第 k..n-1 行中
选取 |A[i,k]| 最大的行 i 与第 k 行交换，再消元。这使乘子 |L| <= 1，
是实践中向后稳定的标准策略。QR 用 Householder 反射，天然数值稳定，
不需要主元（列主元仅用于秩亏检测，本库用条件数估计代替）。

容差约定
--------
  * 精确奇异：消元过程中某列可选主元的最大绝对值**恰为 0.0**
    （即整列剩余部分全为零，秩亏），判定为 "singular"。
  * 数值病态：分解成功后，用 Hager 1-范数条件数估计器给出
    rcond = 1 / (||A||_1 * ||A^{-1}||_1)。rcond < rcond_tol
    （默认 1e-12，约为双精度 eps≈2.2e-16 的 10^4 倍）判定为
    "ill_conditioned"。病态结果**不会**被标记为成功。
  * 正常求解：rcond >= rcond_tol 且相对残差
    relres = ||b - A x||_2 / (||A||_F ||x||_2 + ||b||_2) < res_tol
    （默认 1e-8）判定为 "ok"；残差不达标会抛出 ResidualError。
"""

import math
from dataclasses import dataclass

__all__ = [
    "lu_factor", "lu_solve", "qr_factor", "qr_solve",
    "solve", "lstsq", "SolveResult",
    "SingularError", "IllConditionedError", "ResidualError",
    "matvec", "norm1", "norm2", "normF",
]

DEFAULT_RCOND_TOL = 1e-12
DEFAULT_RES_TOL = 1e-8


class SingularError(ArithmeticError):
    """矩阵精确奇异（出现恰为 0 的主元）。"""


class IllConditionedError(ArithmeticError):
    """矩阵数值病态（rcond 低于容差），结果不可信。"""


class ResidualError(ArithmeticError):
    """相对残差超过给定阈值。"""


@dataclass
class SolveResult:
    x: object        # 解向量（奇异时为 None）
    status: str      # "ok" | "ill_conditioned" | "singular"
    rcond: float     # 估计的 1-范数逆条件数（奇异时为 0.0）
    relres: float    # 相对残差（奇异时为 inf）
    method: str      # "lu" | "qr"
    normal_res: float = 0.0  # 最小二乘的法方程相对残差（方阵求解时为 0）


# ---------------------------------------------------------------- 基础工具

def matvec(A, x):
    return [sum(row[j] * x[j] for j in range(len(x))) for row in A]


def norm1(A):
    """矩阵 1-范数：最大列绝对值和。"""
    m, n = len(A), len(A[0])
    return max(sum(abs(A[i][j]) for i in range(m)) for j in range(n)) if n else 0.0


def norm2(v):
    return math.sqrt(sum(t * t for t in v))


def normF(A):
    return math.sqrt(sum(t * t for row in A for t in row))


def _relres(A, x, b):
    r = [b[i] - v for i, v in enumerate(matvec(A, x))]
    denom = normF(A) * norm2(x) + norm2(b)
    return norm2(r) / denom if denom > 0.0 else norm2(r)


# ---------------------------------------------------------------- LU 分解

def lu_factor(A):
    """带部分主元的 LU 分解：P A = L U。

    返回 (LU, piv, singular_col)：
      LU  : 紧凑存储，下三角为 L（单位对角线不显式存），上三角为 U；
      piv : 第 k 步将第 k 行与第 piv[k] 行交换（piv[k] >= k）；
      singular_col : 若第 k 列可选主元最大绝对值恰为 0.0（精确奇异），
                     返回该列号 k，否则为 -1。
    """
    n = len(A)
    if any(len(row) != n for row in A):
        raise ValueError("lu_factor 要求方阵")
    LU = [list(map(float, row)) for row in A]
    piv = list(range(n))
    for k in range(n):
        p, amax = k, abs(LU[k][k])
        for i in range(k + 1, n):
            a = abs(LU[i][k])
            if a > amax:
                amax, p = a, i
        if amax == 0.0:                      # 精确奇异：整列剩余全为 0
            return LU, piv, k
        if p != k:
            LU[k], LU[p] = LU[p], LU[k]
        piv[k] = p
        for i in range(k + 1, n):
            LU[i][k] /= LU[k][k]
            lik = LU[i][k]
            rowi, rowk = LU[i], LU[k]
            for j in range(k + 1, n):
                rowi[j] -= lik * rowk[j]
    return LU, piv, -1


def lu_solve(LU, piv, b):
    """用 lu_factor 的结果解 A x = b（前代 Ly=Pb，回代 Ux=y）。"""
    n = len(LU)
    x = list(map(float, b))
    for k in range(n):                       # 施加 P
        if piv[k] != k:
            x[k], x[piv[k]] = x[piv[k]], x[k]
    for i in range(1, n):                    # 前代（L 单位对角）
        s = x[i]
        rowi = LU[i]
        for j in range(i):
            s -= rowi[j] * x[j]
        x[i] = s
    for i in range(n - 1, -1, -1):           # 回代
        s = x[i]
        rowi = LU[i]
        for j in range(i + 1, n):
            s -= rowi[j] * x[j]
        x[i] = s / rowi[i]
    return x


def _lu_solve_transpose(LU, piv, b):
    """解 A^T z = b。A = P^T L U  =>  A^T = U^T L^T P。"""
    n = len(LU)
    z = list(map(float, b))
    for i in range(n):                       # U^T w = b（下三角，前代）
        s = z[i]
        for j in range(i):
            s -= LU[j][i] * z[j]
        z[i] = s / LU[i][i]
    for i in range(n - 2, -1, -1):           # L^T v = w（单位对角上三角）
        s = z[i]
        for j in range(i + 1, n):
            s -= LU[j][i] * z[j]
        z[i] = s
    for k in range(n - 1, -1, -1):           # 施加 P^T（逆序交换）
        if piv[k] != k:
            z[k], z[piv[k]] = z[piv[k]], z[k]
    return z


def _rcond_from_lu(LU, piv, normA1):
    """Hager 1-范数条件数估计（Higham, SIAM JSSC 1988 的简化版）。

    通过若干次 A y = x 与 A^T z = xi 的交替求解估计 ||A^{-1}||_1，
    返回 rcond = 1 / (||A||_1 * ||A^{-1}||_1)。代价 O(n^2)。
    """
    n = len(LU)
    if normA1 == 0.0:
        return 0.0
    x = [1.0 / n] * n
    gamma = 0.0
    for _ in range(6):
        y = lu_solve(LU, piv, x)
        gamma = sum(abs(t) for t in y)
        xi = [1.0 if t >= 0.0 else -1.0 for t in y]
        z = _lu_solve_transpose(LU, piv, xi)
        j = max(range(n), key=lambda i: abs(z[i]))
        if abs(z[j]) <= sum(z[i] * x[i] for i in range(n)):
            break
        x = [0.0] * n
        x[j] = 1.0
    return 1.0 / (normA1 * gamma) if gamma > 0.0 else 0.0


# ---------------------------------------------------------------- QR 分解

def qr_factor(A):
    """Householder QR 分解（要求 m >= n），返回薄 Q (m x n) 与 R (n x n)。

    每步用反射 H = I - 2 v v^T（v 单位化）消去对角线以下元素，
    反射方向取 alpha = -sign(a_kk) * ||x|| 以避免相消。
    """
    m, n = len(A), len(A[0])
    if m < n:
        raise ValueError("qr_factor 要求 m >= n（欠定问题请转置后处理）")
    R = [list(map(float, row)) for row in A]
    V = []
    for k in range(n):
        xnorm = math.sqrt(sum(R[i][k] ** 2 for i in range(k, m)))
        if xnorm == 0.0:
            v = [0.0] * (m - k)
        else:
            alpha = -math.copysign(xnorm, R[k][k])
            v = [R[i][k] for i in range(k, m)]
            v[0] -= alpha
            vn = math.sqrt(sum(t * t for t in v))
            v = [t / vn for t in v]
            for j in range(k, n):
                s = sum(v[i - k] * R[i][j] for i in range(k, m))
                for i in range(k, m):
                    R[i][j] -= 2.0 * s * v[i - k]
        V.append(v)
    Q = [[0.0] * n for _ in range(m)]        # 薄 Q：把反射依次作用于 I 的前 n 列
    for j in range(n):
        e = [0.0] * m
        e[j] = 1.0
        for k in range(n - 1, -1, -1):
            v = V[k]
            s = sum(v[i - k] * e[i] for i in range(k, m))
            for i in range(k, m):
                e[i] -= 2.0 * s * v[i - k]
        for i in range(m):
            Q[i][j] = e[i]
    R = [row[:] for row in R[:n]]           # 截断为 n x n 上三角
    return Q, R


def qr_solve(Q, R, b):
    """用 qr_factor 的结果求最小二乘解 x = R^{-1} Q^T b。

    返回 (x, singular_col)：R 对角线恰为 0.0 时 singular_col 为其行号。
    """
    m, n = len(Q), len(R)
    y = [sum(Q[i][j] * b[i] for i in range(m)) for j in range(n)]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        if R[i][i] == 0.0:                   # 精确奇异
            return None, i
        s = y[i] - sum(R[i][j] * x[j] for j in range(i + 1, n))
        x[i] = s / R[i][i]
    return x, -1


# ---------------------------------------------------------------- 高层接口

def solve(A, b, rcond_tol=DEFAULT_RCOND_TOL, res_tol=DEFAULT_RES_TOL,
          strict=False):
    """解方阵方程组 A x = b，返回 SolveResult，健康度三分类：

      "singular"        : 精确奇异（主元恰为 0），x 为 None；
      "ill_conditioned" : 数值病态，rcond < rcond_tol，x 仅供参考；
      "ok"              : 正常求解，且相对残差 < res_tol。

    strict=True 时非 "ok" 直接抛异常；残差不达标抛 ResidualError。
    """
    LU, piv, sing = lu_factor(A)
    if sing >= 0:
        res = SolveResult(None, "singular", 0.0, math.inf, "lu")
        if strict:
            raise SingularError(f"矩阵精确奇异（第 {sing} 列主元为 0）")
        return res
    rcond = _rcond_from_lu(LU, piv, norm1(A))
    x = lu_solve(LU, piv, b)
    relres = _relres(A, x, b)
    if rcond < rcond_tol:
        res = SolveResult(x, "ill_conditioned", rcond, relres, "lu")
        if strict:
            raise IllConditionedError(f"数值病态：rcond={rcond:.3e}")
        return res
    if relres >= res_tol:
        raise ResidualError(f"相对残差 {relres:.3e} >= 阈值 {res_tol:.1e}")
    return SolveResult(x, "ok", rcond, relres, "lu")


def lstsq(A, b, rcond_tol=DEFAULT_RCOND_TOL, res_tol=DEFAULT_RES_TOL,
          strict=False):
    """最小二乘 min ||A x - b||_2（m >= n），返回 SolveResult，分类同 solve。

    条件数基于 R（cond(A) = cond(R)，因 Q 正交保条件数）。
    注意：对不相容超定系统，||Ax-b|| 本身不必很小，残差阈值作用于
    法方程相对残差 ||A^T(Ax-b)|| / (||A||_F^2 ||x|| + ||A||_F ||b||)，
    它衡量的是解的向后稳定性而非拟合误差。
    """
    Q, R = qr_factor(A)
    x, sing = qr_solve(Q, R, b)
    if sing >= 0:
        res = SolveResult(None, "singular", 0.0, math.inf, "qr")
        if strict:
            raise SingularError(f"矩阵精确奇异（R[{sing}][{sing}] = 0）")
        return res
    LU, piv, _ = lu_factor(R)                # R 非奇异，lu 必成功
    rcond = _rcond_from_lu(LU, piv, norm1(R))
    relres = _relres(A, x, b)
    if rcond < rcond_tol:
        res = SolveResult(x, "ill_conditioned", rcond, relres, "qr", math.nan)
        if strict:
            raise IllConditionedError(f"数值病态：rcond={rcond:.3e}")
        return res
    Ax = matvec(A, x)
    grad = [sum(A[i][j] * (Ax[i] - b[i]) for i in range(len(A)))
            for j in range(len(R))]
    nres = norm2(grad) / (normF(A) ** 2 * norm2(x) + normF(A) * norm2(b))
    if nres >= res_tol:
        raise ResidualError(f"法方程相对残差 {nres:.3e} >= 阈值 {res_tol:.1e}")
    return SolveResult(x, "ok", rcond, relres, "qr", nres)
