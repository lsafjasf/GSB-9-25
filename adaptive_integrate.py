"""
adaptive_integrate.py -- 自适应数值积分库（仅依赖 Python 标准库）

算法
----
* 基本公式：Gauss7-Kronrod15 对（与 QUADPACK 的 QK15 相同的节点/权重）。
  Kronrod 结果作为积分值，|Kronrod - Gauss| 经 QUADPACK 风格的
  resasc 修正与舍入下界保护后作为**可信的绝对误差估计**。
* 自适应策略：全局优先（每次二分当前误差最大的子区间），
  误差总量 <= max(atol, rtol*|I|) 时判定收敛。
* 端点奇性：Gauss-Kronrod 为开公式，**从不采样端点**，
  因此可积的端点奇性（如 1/sqrt(x)、log(x)）可直接积分，
  端点处函数值为 inf/nan 也没有影响（只要不被显式求值）。
* 无穷端点：通过变量替换支持 [a, +inf)、(-inf, b]、(-inf, +inf)。
* 区间反序：自动交换端点并对结果取负（见 integrate 文档）。

未收敛语义
----------
达到 max_depth / max_neval / max_intervals 上限仍未满足容差时，
返回 IntegralResult(converged=False)，value 为当前最优估计，
(value - error, value + error) 为不确定性区间，绝不静默当作成功。
被积函数在内部点求值得到非有限值（inf/nan/除零）时同样按未收敛报告。

运行自测：python3 test_adaptive_integrate.py
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Gauss7-Kronrod15 节点与权重（QUADPACK dqk15 的标准数据）
# --------------------------------------------------------------------------
_XGK = (  # Kronrod 节点（正半轴，含 0）
    0.9914553711208126,
    0.9491079123427585,
    0.8648644233597691,
    0.7415311855993944,
    0.5860872354676911,
    0.4058451513773972,
    0.2077849550078985,
    0.0,
)
_WGK = (  # 对应的 Kronrod 权重
    0.02293532201052922,
    0.06309209262997855,
    0.1047900103222502,
    0.1406532597155259,
    0.1690047266392679,
    0.1903505780647854,
    0.2044329400752989,
    0.2094821410847278,
)
_WG = (  # Gauss7 权重，对应 _XGK 的下标 1, 3, 5, 7
    0.1294849661688697,
    0.2797053914892767,
    0.3818300505051189,
    0.4179591836734694,
)

_EPMACH = 2.220446049250313e-16   # 双精度机器 epsilon
_UFLOW = 2.2250738585072014e-308  # 最小正规格化双精度数


class NonFiniteEvaluation(Exception):
    """被积函数在内部点产生非有限值（inf/nan）或除零。"""

    def __init__(self, x: float):
        super().__init__(f"integrand evaluated to a non-finite value at x={x!r}")
        self.x = x


@dataclass
class IntegralResult:
    """积分结果。converged=False 时 value/error 仍为当前最优估计与不确定性半径。"""

    value: float          # 积分估计值
    error: float          # 绝对误差估计（不确定性半径，可为 inf）
    converged: bool       # 是否满足容差
    neval: int            # 被积函数求值次数
    nintervals: int       # 最终细分子区间数
    elapsed: float        # 耗时（秒）
    message: str = ""     # 未收敛原因等附加信息

    @property
    def uncertainty(self) -> tuple[float, float]:
        """不确定性区间 [value - error, value + error]。"""
        return (self.value - self.error, self.value + self.error)


def _gk15(f, a: float, b: float) -> tuple[float, float]:
    """在 [a,b] 上应用 Gauss7-Kronrod15，返回 (积分值, 误差估计)。f 计 15 次求值。"""
    hlgth = 0.5 * (b - a)
    centr = 0.5 * (a + b)

    fc = f(centr)
    resg = _WG[3] * fc
    resk = _WGK[7] * fc
    resabs = abs(resk)
    fv1 = [0.0] * 7
    fv2 = [0.0] * 7

    for j in range(3):  # Kronrod 节点 1,3,5（同时是 Gauss 节点）
        jw = 2 * j + 1
        absc = hlgth * _XGK[jw]
        f1 = f(centr - absc)
        f2 = f(centr + absc)
        fv1[jw] = f1
        fv2[jw] = f2
        fsum = f1 + f2
        resg += _WG[j] * fsum
        resk += _WGK[jw] * fsum
        resabs += _WGK[jw] * (abs(f1) + abs(f2))
    for j in range(4):  # Kronrod 节点 0,2,4,6
        jw = 2 * j
        absc = hlgth * _XGK[jw]
        f1 = f(centr - absc)
        f2 = f(centr + absc)
        fv1[jw] = f1
        fv2[jw] = f2
        fsum = f1 + f2
        resk += _WGK[jw] * fsum
        resabs += _WGK[jw] * (abs(f1) + abs(f2))

    reskh = resk * 0.5
    resasc = _WGK[7] * abs(fc - reskh)
    for j in range(7):
        resasc += _WGK[j] * (abs(fv1[j] - reskh) + abs(fv2[j] - reskh))

    result = resk * hlgth
    resabs *= abs(hlgth)
    resasc *= abs(hlgth)
    err = abs((resk - resg) * hlgth)
    if resasc != 0.0 and err != 0.0:
        err = resasc * min(1.0, (200.0 * err / resasc) ** 1.5)
    if resabs > _UFLOW / (50.0 * _EPMACH):
        err = max(50.0 * _EPMACH * resabs, err)  # 舍入下界保护
    return result, err


def _make_counter(f):
    """包装被积函数：计数求值次数，并把非有限值/除零转成 NonFiniteEvaluation。"""
    count = [0]

    def wrapped(x: float) -> float:
        count[0] += 1
        try:
            y = f(x)
        except (ZeroDivisionError, OverflowError):
            raise NonFiniteEvaluation(x) from None
        if not math.isfinite(y):
            raise NonFiniteEvaluation(x)
        return y

    return wrapped, count


def _transform(f, a: float, b: float):
    """把无穷区间变换为有限区间 [0,1] 上的积分，返回 (g, 0, 1) 或 (f, a, b)。"""
    if math.isinf(a) and math.isinf(b):
        raise ValueError("both bounds infinite is handled by integrate() itself")
    if math.isinf(b):  # [a, +inf): x = a + (1-t)/t, dx = dt / t^2
        def g(t: float) -> float:
            return f(a + (1.0 - t) / t) / (t * t)
        return g, 0.0, 1.0
    if math.isinf(a):  # (-inf, b]: x = b - (1-t)/t, dx = dt / t^2
        def g(t: float) -> float:
            return f(b - (1.0 - t) / t) / (t * t)
        return g, 0.0, 1.0
    return f, a, b


def _integrate_finite(f, a, b, atol, rtol, max_depth, max_neval, max_intervals):
    """有限区间上的自适应积分主循环（f 已包装计数）。"""
    value0, err0 = _gk15(f, a, b)
    total_val, total_err = value0, err0
    # 堆元素：(-err, 序号, a, b, value, err, level)
    heap = [(-err0, 0, a, b, value0, err0, 0)]
    nintervals = 1
    serial = 1
    hit_depth = False
    neval_used = 15

    def tol() -> float:
        return max(atol, rtol * abs(total_val))

    while total_err > tol():
        if neval_used + 30 > max_neval:
            return total_val, total_err, False, nintervals, neval_used, \
                f"evaluation limit reached (max_neval={max_neval})"
        if not heap:
            return total_val, total_err, False, nintervals, neval_used, \
                f"no bisectable subinterval left (max_depth={max_depth})"
        if nintervals + 1 > max_intervals:
            return total_val, total_err, False, nintervals, neval_used, \
                f"subinterval limit reached (max_intervals={max_intervals})"

        _, _, ia, ib, ival, ierr, level = heapq.heappop(heap)
        if level >= max_depth:
            hit_depth = True
            continue  # 冻结该区间：贡献保留在总量中，但不再二分
        mid = 0.5 * (ia + ib)
        if mid == ia or mid == ib:  # 浮点不可再分
            hit_depth = True
            continue

        v1, e1 = _gk15(f, ia, mid)
        v2, e2 = _gk15(f, mid, ib)
        neval_used += 30
        total_val += v1 + v2 - ival
        total_err += e1 + e2 - ierr
        heapq.heappush(heap, (-e1, serial, ia, mid, v1, e1, level + 1))
        serial += 1
        heapq.heappush(heap, (-e2, serial, mid, ib, v2, e2, level + 1))
        serial += 1
        nintervals += 1

    msg = "max_depth reached on some subinterval(s)" if hit_depth else ""
    return total_val, total_err, True, nintervals, neval_used, msg


def integrate(f, a: float, b: float, *, atol: float = 1e-10, rtol: float = 1e-10,
              max_depth: int = 30, max_neval: int = 100_000,
              max_intervals: int = 10_000) -> IntegralResult:
    """
    自适应计算 \\int_a^b f(x) dx。

    参数
    ----
    atol, rtol   : 绝对/相对容差，收敛判据 err <= max(atol, rtol*|I|)
    max_depth    : 单个子区间的最大二分深度
    max_neval    : 被积函数最大求值次数
    max_intervals: 最大子区间数

    语义
    ----
    * 区间反序（a > b）：自动交换端点，结果取负。
    * 无穷端点：支持 [a, +inf)、(-inf, b]、(-inf, +inf)（内部在 0 处拆分）。
    * 端点奇性：开公式不采样端点，可积端点奇性可直接积分。
    * 未收敛：返回 converged=False，value/error 给出当前估计与不确定性区间。
    """
    if atol < 0 or rtol < 0:
        raise ValueError("atol/rtol must be non-negative")
    if atol == 0 and rtol == 0:
        raise ValueError("atol and rtol cannot both be zero")
    if max_depth < 0:
        raise ValueError("max_depth must be >= 0")

    t0 = time.perf_counter()

    if a == b:
        return IntegralResult(0.0, 0.0, True, 0, 0, 0.0, "degenerate interval")

    sign = 1.0
    if a > b:  # 区间反序：自动纠正（交换端点并取负）
        a, b = b, a
        sign = -1.0

    counter_f, count = _make_counter(f)

    # (-inf, +inf)：在 0 处拆成两个半无穷区间，容差对半
    if math.isinf(a) and math.isinf(b):
        if not (a < 0.0 < b):
            raise ValueError("invalid infinite bounds")
        r1 = integrate(f, 0.0, b, atol=atol / 2, rtol=rtol,
                       max_depth=max_depth, max_neval=max_neval,
                       max_intervals=max_intervals)
        r2 = integrate(f, a, 0.0, atol=atol / 2, rtol=rtol,
                       max_depth=max_depth, max_neval=max_neval,
                       max_intervals=max_intervals)
        elapsed = time.perf_counter() - t0
        msgs = [m for m in (r1.message, r2.message) if m]
        return IntegralResult(
            value=r1.value + r2.value,
            error=r1.error + r2.error,
            converged=r1.converged and r2.converged,
            neval=r1.neval + r2.neval,
            nintervals=r1.nintervals + r2.nintervals,
            elapsed=elapsed,
            message="; ".join(msgs),
        )

    try:
        g, lo, hi = _transform(counter_f, a, b)
        value, err, ok, nint, _, msg = _integrate_finite(
            g, lo, hi, atol, rtol, max_depth, max_neval, max_intervals)
    except NonFiniteEvaluation as exc:
        elapsed = time.perf_counter() - t0
        return IntegralResult(
            value=math.nan, error=math.inf, converged=False,
            neval=count[0], nintervals=0, elapsed=elapsed,
            message=f"non-finite integrand value: {exc}",
        )

    elapsed = time.perf_counter() - t0
    return IntegralResult(
        value=sign * value, error=err, converged=ok,
        neval=count[0], nintervals=nint, elapsed=elapsed, message=msg,
    )


def fixed_simpson(f, a: float, b: float, n: int = 1024) -> tuple[float, int]:
    """
    等距固定细分的复合 Simpson（对照方法，仅支持有限区间）。
    返回 (积分值, 求值次数)。n 为子区间数（自动取偶）。
    """
    if n % 2:
        n += 1
    h = (b - a) / n
    s = f(a) + f(b)
    for i in range(1, n):
        s += (4.0 if i % 2 else 2.0) * f(a + i * h)
    return s * h / 3.0, n + 1
