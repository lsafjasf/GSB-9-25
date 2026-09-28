"""自适应数值积分库（仅标准库）。

核心算法：Gauss-Kronrod 15 点求积（内嵌 7 点 Gauss 结果做误差估计），
配合最大堆驱动的自适应二分细分（每次细分误差最大的子区间）。

特性：
- 绝对/相对误差容差（epsabs / epsrel）
- 最大细分深度（max_depth）与最大函数求值次数（max_evals）
- 端点奇性：GK 公式为开公式，从不在端点求值，因此端点可积奇性
  （如 x**-0.5、log(x)）可直接处理；可选 endpoint_transform=True
  用多项式变换把节点向端点聚集以加速强奇性收敛
- 区间反序：自动交换并取负（见 integrate 文档）
- 无穷端点：支持 (a, inf)、(-inf, b)、(-inf, inf)，通过变量替换化为有限区间
- 未收敛：不抛异常、不静默返回，而是返回 converged=False 的 Result，
  其中包含当前最优估计与不确定性区间
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass

# Gauss-Kronrod 7-15 节点与权重（QUADPACK qk15，标准数值）
_XGK = (
    0.9914553711208126,
    0.9491079123427585,
    0.8648644233597691,
    0.7415311855993944,
    0.5860872354676911,
    0.4058451513773972,
    0.2077849550078985,
    0.0,
)
_WGK = (
    0.02293532201052922,
    0.06309209262997855,
    0.1047900103222502,
    0.1406532597155259,
    0.1690047266392679,
    0.1903505780647854,
    0.2044329400752989,
    0.2094821410847278,
)
_WG = (
    0.1294849661688697,
    0.2797053914892767,
    0.3818300505051189,
    0.4179591836734694,
)

_EVALS_PER_PANEL = 15


def _qk15(f, a, b):
    """在 [a, b] 上计算 GK15 积分值与误差估计，返回 (value, err, neval)。"""
    center = 0.5 * (a + b)
    half = 0.5 * (b - a)
    fc = f(center)
    resg = _WG[3] * fc
    resk = _WGK[7] * fc
    for j in range(7):
        absc = half * _XGK[j]
        fval = f(center - absc) + f(center + absc)
        resk += _WGK[j] * fval
        if j % 2 == 1:
            resg += _WG[j // 2] * fval
    value = resk * half
    gauss = resg * half
    err = abs(value - gauss)
    if not math.isfinite(err):
        err = math.inf
    return value, err, _EVALS_PER_PANEL


@dataclass
class Result:
    """积分结果。

    converged=False 表示未达容差（触及 max_depth 或 max_evals），
    此时 value 为当前最优估计，error 为当前不确定性估计，
    interval 为 (value - error, value + error)。
    """

    value: float
    error: float
    converged: bool
    neval: int          # 函数求值次数
    nsub: int           # 最终细分子区间数
    elapsed: float      # 耗时（秒）
    message: str

    @property
    def interval(self):
        return (self.value - self.error, self.value + self.error)

    def __repr__(self):
        flag = "OK " if self.converged else "FAIL"
        return (
            f"<Result {flag} value={self.value:.16g} err={self.error:.3g} "
            f"neval={self.neval} nsub={self.nsub} "
            f"elapsed={self.elapsed * 1e3:.3f}ms msg={self.message!r}>"
        )


def _wrap_infinite(f, a, b):
    """把含无穷端点的积分变换为 (0,1) 上的积分，返回 (g, 0, 1)。"""
    if math.isinf(a) and math.isinf(b):
        if a > 0 or b < 0:
            raise ValueError("infinite bounds have wrong signs")
        half_pi = math.pi / 2.0

        def g(t):
            u = half_pi * (2.0 * t - 1.0)
            x = math.tan(u)
            return f(x) * half_pi * (1.0 + x * x) * 2.0

        return g, 0.0, 1.0
    if math.isinf(b):
        if b < 0:
            raise ValueError("b = -inf with finite a is not supported; swap bounds")
        base = a

        def g(t):
            s = 1.0 - t
            x = base + t / s
            return f(x) / (s * s)

        return g, 0.0, 1.0
    if math.isinf(a):
        if a > 0:
            raise ValueError("a = +inf with finite b is not supported; swap bounds")
        base = b

        def g(t):
            s = 1.0 - t
            x = base - t / s
            return f(x) / (s * s)

        return g, 0.0, 1.0
    return f, a, b


def _endpoint_transform(f, a, b):
    """Kahaner 多项式变换 x = a + (b-a) * t^2 (3 - 2t)，

    使节点向两端聚集，且 dx/dt 在端点为 0，可压制端点奇性。
    """
    span = b - a

    def g(t):
        x = a + span * t * t * (3.0 - 2.0 * t)
        return f(x) * span * 6.0 * t * (1.0 - t)

    return g, 0.0, 1.0


def integrate(
    f,
    a,
    b,
    epsabs=1e-10,
    epsrel=1e-10,
    max_depth=30,
    max_evals=100_000,
    endpoint_transform=False,
):
    """自适应积分主入口。

    参数：
        f: 被积函数 f(x) -> float
        a, b: 积分区间端点；允许 a > b（自动交换并对结果取负）；
              允许 ±math.inf（自动做变量替换）
        epsabs, epsrel: 绝对/相对容差，收敛判据为
              err <= max(epsabs, epsrel * |value|)
        max_depth: 单个子区间的最大二分深度
        max_evals: 函数求值次数上限
        endpoint_transform: 是否对有限区间做端点聚集变换（处理端点奇性）

    返回：Result。未收敛时 converged=False，绝不静默当作成功。
    """
    t0 = time.perf_counter()
    sign = 1.0
    reversed_interval = False
    if b < a:
        a, b = b, a
        sign = -1.0
        reversed_interval = True
    if a == b:
        return Result(0.0, 0.0, True, 0, 0, time.perf_counter() - t0, "empty interval")

    g, lo, hi = _wrap_infinite(f, a, b)
    infinite = (lo, hi) != (a, b)
    if endpoint_transform and not infinite:
        g, lo, hi = _endpoint_transform(g, lo, hi)

    value0, err0, n = _qk15(g, lo, hi)
    neval = n
    # 被积函数在采样点返回非有限值（inf/nan）：无法构造有意义的
    # 误差估计，直接走未收敛报告路径，绝不当作收敛返回。
    if not (math.isfinite(value0) and math.isfinite(err0)):
        return Result(
            sign * value0, math.inf, False, neval, 1,
            time.perf_counter() - t0,
            "integrand returned non-finite value at sample points",
        )
    # 堆元素：(-err, seq, a, b, value, err, depth)
    heap = [(-err0, 0, lo, hi, value0, err0, 0)]
    seq = 1
    total = value0
    total_err = err0
    nsub = 1
    message = "converged"
    converged = True

    while total_err > max(epsabs, epsrel * abs(total)):
        if neval + 2 * _EVALS_PER_PANEL > max_evals:
            converged = False
            message = f"max_evals reached ({max_evals}); error estimate {total_err:.3g} exceeds tolerance"
            break
        if not heap:
            # 所有子区间均已到顶被冻结，误差仍超容差。
            converged = False
            message = f"max_depth reached ({max_depth}); error estimate {total_err:.3g} exceeds tolerance"
            break
        neg_err, _, sa, sb, sval, serr, depth = heapq.heappop(heap)
        if depth >= max_depth:
            # 只冻结该子区间：其值与误差贡献保留在 total/total_err 中，
            # 但不再细分；更浅的子区间仍可继续压低总误差。
            continue
        mid = 0.5 * (sa + sb)
        v1, e1, n1 = _qk15(g, sa, mid)
        v2, e2, n2 = _qk15(g, mid, sb)
        neval += n1 + n2
        total += (v1 + v2) - sval
        total_err += (e1 + e2) - serr
        if not (math.isfinite(total) and math.isfinite(total_err)):
            # 细分后采样点命中非有限值（如内部奇点），走未收敛报告路径。
            converged = False
            message = "integrand returned non-finite value at sample points"
            break
        heapq.heappush(heap, (-e1, seq, sa, mid, v1, e1, depth + 1))
        seq += 1
        heapq.heappush(heap, (-e2, seq, mid, sb, v2, e2, depth + 1))
        seq += 1
        nsub += 1

    value = sign * total
    # 误差估计加入浮点舍入下界：任何估计都不会比机器精度更准，
    # 保证报告的 err 在机器噪声量级上仍覆盖真实误差。
    err = abs(total_err) + 8.0 * 2.220446049250313e-16 * max(1.0, abs(total))
    if reversed_interval:
        message += " (interval was reversed: swapped and negated)"
    return Result(value, err, converged, neval, nsub, time.perf_counter() - t0, message)


# ---------------------------------------------------------------------------
# 对照组：前缀固定（均匀）细分 + Gauss-Legendre 4 点，倍增加密直到
# Richardson 估计 |I_2N - I_N| 满足容差。用于求值次数对比。
# ---------------------------------------------------------------------------

_GL4_X = (0.8611363115940526, 0.3399810435848563)
_GL4_W = (0.3478548451374538, 0.6521451548625461)


def _gl4_panel(f, a, b):
    c = 0.5 * (a + b)
    h = 0.5 * (b - a)
    s = 0.0
    for x, w in zip(_GL4_X, _GL4_W):
        s += w * (f(c - h * x) + f(c + h * x))
    return s * h


def fixed_integrate(f, a, b, epsabs=1e-10, epsrel=1e-10, max_panels=1 << 18):
    """均匀 N 等分 + GL4，N 从 1 起倍增直到容差满足。返回 Result。

    仅支持有限区间（对照用途）。
    """
    t0 = time.perf_counter()
    sign = 1.0
    if b < a:
        a, b = b, a
        sign = -1.0
    g, lo, hi = _wrap_infinite(f, a, b)

    def composite(npanels):
        h = (hi - lo) / npanels
        total = 0.0
        for i in range(npanels):
            total += _gl4_panel(g, lo + i * h, lo + (i + 1) * h)
        return total

    n = 1
    prev = composite(n)
    neval = 4 * n
    while n < max_panels:
        n *= 2
        cur = composite(n)
        neval += 4 * n
        err = abs(cur - prev)
        if err <= max(epsabs, epsrel * abs(cur)):
            return Result(sign * cur, err, True, neval, n, time.perf_counter() - t0, "converged")
        prev = cur
    return Result(
        sign * prev, abs(prev), False, neval, n,
        time.perf_counter() - t0, "fixed method did not converge",
    )
