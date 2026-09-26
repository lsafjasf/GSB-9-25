"""matrix_decomp.py — 纯标准库矩阵分解与线性求解库。

功能:
  * LU 分解(部分主元)  —— lu_factor / lu_solve
  * Householder QR 分解 —— qr_factor / qr_solve (支持 m>=n 最小二乘)
  * 统一求解接口 solve()，结论区分三类:
        SolveStatus.OK              正常求解
        SolveStatus.EXACT_SINGULAR  精确奇异(主元精确为 0 / R 对角线精确为 0)
        SolveStatus.ILL_CONDITIONED 数值病态(条件数估计超过阈值, 不视为成功)
  * 条件数估计(1-范数, 基于 LU 显式求逆列) condition_estimate_1
  * 相对残差 relative_residual = ||Ax-b||2 / (||A||F*||x||2 + ||b||2)

主元选择规则(LU):
  第 k 步在第 k 列的第 k..n-1 行中选取 |a_ik| 最大者作为主元行并交换
  (部分主元, partial pivoting)。主元容差:
      pivot_tol = n * EPS * max|A|     (EPS 为双精度机器 epsilon)
  主元 *精确等于 0.0* 时判定精确奇异; 主元 <= pivot_tol 时记录告警信息,
  是否病态最终由条件数估计判定(阈值 cond_threshold, 默认 1e12)。

QR 主元/容差:
  Householder 反射无需选主元; 秩判定容差
      rank_tol = max(m,n) * EPS * max|diag(R)| 之前的尺度, 即
      |r_kk| <= max(m,n) * EPS * ||A||F 视为数值秩亏;
  r_kk 精确为 0.0 判定精确奇异。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

EPS = 2.220446049250313e-16  # 双精度机器 epsilon

#: 条件数估计超过该阈值即判定为数值病态(约损失 12 位以上有效数字)
DEFAULT_COND_THRESHOLD = 1.0e12

Matrix = Sequence[Sequence[float]]
Vector = Sequence[float]


class SolveStatus(Enum):
    OK = "ok"
    EXACT_SINGULAR = "exact_singular"
    ILL_CONDITIONED = "ill_conditioned"


@dataclass
class SolveResult:
    status: SolveStatus
    x: Optional[list]                 # 解(病态时仍给出供参考, 精确奇异时为 None)
    condition_estimate: float         # 1-范数条件数估计(精确奇异时为 inf)
    relative_residual: float          # 相对残差(精确奇异时为 nan)
    method: str
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is SolveStatus.OK


# ---------------------------------------------------------------- 基础工具

def _check_rectangular(A: Matrix) -> tuple[int, int]:
    m = len(A)
    if m == 0:
        raise ValueError("矩阵不能为空")
    n = len(A[0])
    if n == 0:
        raise ValueError("矩阵列数不能为空")
    for row in A:
        if len(row) != n:
            raise ValueError("矩阵各行长度不一致")
    return m, n


def norm_fro(A: Matrix) -> float:
    return math.sqrt(sum(v * v for row in A for v in row))


def norm_1(A: Matrix) -> float:
    m, n = _check_rectangular(A)
    return max(sum(abs(A[i][j]) for i in range(m)) for j in range(n))


def norm_2_vec(x: Vector) -> float:
    return math.sqrt(sum(v * v for v in x))


def mat_vec(A: Matrix, x: Vector) -> list:
    return [sum(row[j] * x[j] for j in range(len(x))) for row in A]


def relative_residual(A: Matrix, x: Vector, b: Vector) -> float:
    """相对残差 ||b - Ax||2 / (||A||F*||x||2 + ||b||2)。"""
    r = [bi - ai for bi, ai in zip(b, mat_vec(A, x))]
    denom = norm_fro(A) * norm_2_vec(x) + norm_2_vec(b)
    if denom == 0.0:
        return 0.0 if norm_2_vec(r) == 0.0 else math.inf
    return norm_2_vec(r) / denom


# ---------------------------------------------------------------- LU 分解

def lu_factor(A: Matrix, pivot_tol: Optional[float] = None) -> tuple[list, list, dict]:
    """部分主元 LU 分解: P*A = L*U。

    返回 (LU, perm, info):
      LU   —— 压缩存储, 下三角为 L(对角线隐含为 1), 上三角为 U
      perm —— 行置换, 第 k 步交换了第 k 行与第 perm[k] 行
      info —— {"exact_singular": bool, "pivot_tol": float,
               "min_pivot_ratio": float, "tiny_pivot": bool}
    """
    n, n2 = _check_rectangular(A)
    if n != n2:
        raise ValueError("lu_factor 需要方阵")
    LU = [list(map(float, row)) for row in A]
    perm = list(range(n))
    max_abs = max((abs(v) for row in A for v in row), default=0.0)
    if pivot_tol is None:
        pivot_tol = n * EPS * max_abs
    info = {
        "exact_singular": False,
        "pivot_tol": pivot_tol,
        "min_pivot_ratio": math.inf,
        "tiny_pivot": False,
    }
    for k in range(n):
        # 部分主元: 选第 k 列第 k..n-1 行中绝对值最大者
        p = max(range(k, n), key=lambda i: abs(LU[i][k]))
        piv = abs(LU[p][k])
        if piv == 0.0:
            info["exact_singular"] = True
            info["singular_step"] = k
            return LU, perm, info
        if max_abs > 0.0:
            ratio = piv / max_abs
            if ratio < info["min_pivot_ratio"]:
                info["min_pivot_ratio"] = ratio
        if piv <= pivot_tol:
            info["tiny_pivot"] = True
        if p != k:
            LU[k], LU[p] = LU[p], LU[k]
        perm[k] = p
        inv = 1.0 / LU[k][k]
        for i in range(k + 1, n):
            LU[i][k] *= inv
            f = LU[i][k]
            if f != 0.0:
                row_i, row_k = LU[i], LU[k]
                for j in range(k + 1, n):
                    row_i[j] -= f * row_k[j]
    return LU, perm, info


def lu_solve(LU: list, perm: list, b: Vector) -> list:
    """用 lu_factor 的结果解 A*x = b (b 为任意右端向量)。"""
    n = len(LU)
    # 按记录的主元交换顺序重排 b
    y = [float(v) for v in b]
    for k in range(n):
        if perm[k] != k:
            y[k], y[perm[k]] = y[perm[k]], y[k]
    # 前代: L*y = Pb (L 单位对角)
    for i in range(1, n):
        s = 0.0
        row = LU[i]
        for j in range(i):
            s += row[j] * y[j]
        y[i] -= s
    # 回代: U*x = y
    for i in range(n - 1, -1, -1):
        s = 0.0
        row = LU[i]
        for j in range(i + 1, n):
            s += row[j] * y[j]
        y[i] = (y[i] - s) / row[i]
    return y


# ---------------------------------------------------------------- QR 分解

def qr_factor(A: Matrix) -> tuple[list, list, list, dict]:
    """Householder QR 分解, 支持 m >= n。

    返回 (R, V, tau, info):
      R   —— m x n 上梯形矩阵(显式)
      V   —— 第 k 个反射向量 v_k (长度 m-k), Q = H_0 H_1 ... H_{n-1}
      tau —— tau_k = 2 / (v_k . v_k)
      info—— {"exact_singular": bool, "rank_tol": float, "tiny_diag": bool}
    """
    m, n = _check_rectangular(A)
    if m < n:
        raise ValueError("qr_factor 需要 m >= n (行数 >= 列数)")
    R = [list(map(float, row)) for row in A]
    V: list[list] = []
    tau: list[float] = []
    rank_tol = max(m, n) * EPS * norm_fro(A)
    info = {"exact_singular": False, "rank_tol": rank_tol, "tiny_diag": False}
    for k in range(n):
        normx = math.sqrt(sum(R[i][k] * R[i][k] for i in range(k, m)))
        if normx == 0.0:
            v = [0.0] * (m - k)
            t = 0.0
        else:
            alpha = -math.copysign(normx, R[k][k])
            v = [R[i][k] for i in range(k, m)]
            v[0] -= alpha
            t = 2.0 / sum(x * x for x in v)
        for j in range(k, n):
            w = t * sum(v[i - k] * R[i][j] for i in range(k, m))
            for i in range(k, m):
                R[i][j] -= v[i - k] * w
        V.append(v)
        tau.append(t)
        d = abs(R[k][k])
        if d == 0.0:
            info["exact_singular"] = True
            info["singular_step"] = k
        elif d <= rank_tol:
            info["tiny_diag"] = True
    return R, V, tau, info


def qr_solve(R: list, V: list, tau: list, b: Vector) -> tuple[list, float]:
    """最小二乘解 min ||A x - b||2。返回 (x, 残差范数 ||b-Ax||2)。"""
    m = len(R)
    n = len(R[0])
    c = [float(v) for v in b]
    # c <- Q^T b
    for k in range(n):
        v, t = V[k], tau[k]
        w = t * sum(v[i - k] * c[i] for i in range(k, m))
        for i in range(k, m):
            c[i] -= v[i - k] * w
    # 回代上三角 R(0:n, 0:n)
    x = c[:n]
    for i in range(n - 1, -1, -1):
        s = sum(R[i][j] * x[j] for j in range(i + 1, n))
        x[i] = (x[i] - s) / R[i][i]
    return x, norm_2_vec(c[n:])


# ---------------------------------------------------------------- 条件数估计

def condition_estimate_1(A: Matrix) -> float:
    """1-范数条件数估计: ||A||_1 * ||A^{-1}||_1。

    ||A^{-1}||_1 通过对每个单位向量 e_j 用 LU 解 A x = e_j 取列和最大值。
    精确奇异时返回 inf。
    """
    n, n2 = _check_rectangular(A)
    if n != n2:
        raise ValueError("condition_estimate_1 需要方阵")
    LU, perm, info = lu_factor(A)
    if info["exact_singular"]:
        return math.inf
    norm_a = norm_1(A)
    norm_inv = 0.0
    for j in range(n):
        e = [0.0] * n
        e[j] = 1.0
        x = lu_solve(LU, perm, e)
        norm_inv = max(norm_inv, sum(abs(v) for v in x))
    return norm_a * norm_inv


# ---------------------------------------------------------------- 统一接口

def solve(A: Matrix, b: Vector, method: str = "lu",
          cond_threshold: float = DEFAULT_COND_THRESHOLD) -> SolveResult:
    """求解 A x = b (方阵) 或最小二乘 min||Ax-b|| (m>=n, method='qr')。

    结论三类:
      EXACT_SINGULAR  —— 主元/R 对角精确为 0, x 为 None
      ILL_CONDITIONED —— 条件数估计 > cond_threshold, x 仍返回但不可信
      OK              —— 正常求解
    """
    m, n = _check_rectangular(A)
    if len(b) != m:
        raise ValueError("右端向量长度与矩阵行数不一致")

    if method == "lu":
        if m != n:
            raise ValueError("LU 求解需要方阵; 非方阵请用 method='qr'")
        LU, perm, info = lu_factor(A)
        if info["exact_singular"]:
            return SolveResult(SolveStatus.EXACT_SINGULAR, None, math.inf,
                               math.nan, method,
                               f"第 {info['singular_step']} 步主元精确为 0")
        x = lu_solve(LU, perm, b)
        cond = condition_estimate_1(A)
    elif method == "qr":
        R, V, tau, info = qr_factor(A)
        if info["exact_singular"]:
            return SolveResult(SolveStatus.EXACT_SINGULAR, None, math.inf,
                               math.nan, method,
                               f"R 第 {info['singular_step']} 个对角元精确为 0")
        x, _ = qr_solve(R, V, tau, b)
        # 条件数估计: 方阵直接估 A; 长方阵估上三角 R (cond2(A)=cond2(R))
        top_R = [row[:] for row in R[:n]]
        cond = condition_estimate_1(top_R)
    else:
        raise ValueError(f"未知方法: {method!r}")

    res = relative_residual(A, x, b)
    if cond > cond_threshold:
        return SolveResult(SolveStatus.ILL_CONDITIONED, x, cond, res, method,
                           f"条件数估计 {cond:.3e} 超过阈值 {cond_threshold:.1e}, "
                           f"解不可信")
    return SolveResult(SolveStatus.OK, x, cond, res, method)
