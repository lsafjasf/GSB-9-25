"""自测脚本：对拍验证 + 未收敛用例 + 求值次数/耗时对比。

运行：python3 selftest.py
退出码非 0 表示有断言失败。
"""

import math
import sys
import time

from adaptive_integrate import integrate, fixed_integrate

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAILURES.append(name)
    print(f"  [{status}] {name} {detail}")


# ---------------------------------------------------------------------------
# 1. 误差估计可验证性：与解析值对拍，要求 |实际误差| <= 报告的误差估计
# ---------------------------------------------------------------------------
print("== 1. 解析对拍：实际误差不得大于报告的误差估计 ==")

CASES = [
    # (名称, f, a, b, 解析值, 额外参数)
    ("多项式 x^5", lambda x: x**5, 0.0, 1.0, 1.0 / 6.0, {}),
    ("光滑 exp(x)", math.exp, 0.0, 1.0, math.e - 1.0, {}),
    ("三角 sin(10x)", lambda x: math.sin(10 * x), 0.0, math.pi,
     (1.0 - math.cos(10 * math.pi)) / 10.0, {}),
    ("有理 1/(1+x^2)", lambda x: 1.0 / (1.0 + x * x), 0.0, 1.0, math.pi / 4.0, {}),
    ("端点奇性 x^-1/2", lambda x: x**-0.5, 0.0, 1.0, 2.0,
     {"endpoint_transform": True}),
    ("端点奇性 log(x)", math.log, 0.0, 1.0, -1.0, {"endpoint_transform": True}),
    ("端点奇性 sqrt(x(1-x))", lambda x: math.sqrt(x * (1.0 - x)), 0.0, 1.0,
     math.pi / 8.0, {"endpoint_transform": True}),
    ("振荡 cos(200x)", lambda x: math.cos(200.0 * x), 0.0, 1.0,
     math.sin(200.0) / 200.0, {}),
    ("尖点 |x-0.3|^0.5", lambda x: abs(x - 0.3) ** 0.5, 0.0, 1.0,
     (0.3 ** 1.5 + 0.7 ** 1.5) / 1.5, {}),
    ("无穷 [0,inf) e^-x", lambda x: math.exp(-x), 0.0, math.inf, 1.0, {}),
    ("无穷 (-inf,inf) e^-x^2", lambda x: math.exp(-x * x),
     -math.inf, math.inf, math.sqrt(math.pi), {}),
    ("无穷 (-inf,inf) 1/(1+x^2)", lambda x: 1.0 / (1.0 + x * x),
     -math.inf, math.inf, math.pi, {}),
]

print(f"  {'函数':<28} {'实际误差':>12} {'报告估计':>12} {'求值次数':>8} {'子区间':>6} {'耗时ms':>8}")
for name, f, a, b, exact, kw in CASES:
    r = integrate(f, a, b, epsabs=1e-9, epsrel=1e-9, **kw)
    actual = abs(r.value - exact)
    ok = r.converged and actual <= r.error
    print(f"  {name:<28} {actual:>12.3e} {r.error:>12.3e} {r.neval:>8} {r.nsub:>6} {r.elapsed*1e3:>8.3f}")
    check(f"对拍 {name}", ok,
          f"(actual={actual:.3e} <= reported={r.error:.3e}, converged={r.converged})")

# ---------------------------------------------------------------------------
# 2. 区间反序
# ---------------------------------------------------------------------------
print("\n== 2. 区间反序（自动交换并取负）==")
r_fwd = integrate(lambda x: math.sin(x), 0.0, 2.0)
r_rev = integrate(lambda x: math.sin(x), 2.0, 0.0)
check("反序结果互为相反数", abs(r_fwd.value + r_rev.value) < 1e-12,
      f"fwd={r_fwd.value:.15g} rev={r_rev.value:.15g}")
check("反序仍收敛", r_rev.converged)

# ---------------------------------------------------------------------------
# 3. 未收敛用例：必须明确报告，不得静默成功
# ---------------------------------------------------------------------------
print("\n== 3. 未收敛用例（max_depth / max_evals）==")

# 3a. 强振荡 + 极浅深度限制
r = integrate(lambda x: math.sin(1.0 / x) if x != 0.0 else 0.0,
              0.0, 1.0, epsabs=1e-12, epsrel=1e-12, max_depth=4)
check("max_depth 触发未收敛", not r.converged, f"msg={r.message!r}")
lo, hi = r.interval
check("未收敛仍返回不确定性区间", lo < hi and math.isfinite(r.value),
      f"interval=({lo:.6g}, {hi:.6g})")

# 3b. 端点奇性 + 极小求值预算
r = integrate(lambda x: x**-0.5, 0.0, 1.0, epsabs=1e-12, epsrel=1e-12,
              max_evals=200)
check("max_evals 触发未收敛", not r.converged, f"msg={r.message!r}")
exact = 2.0
lo, hi = r.interval
check("未收敛区间覆盖真值", lo <= exact <= hi,
      f"interval=({lo:.6g}, {hi:.6g}), exact={exact}")

# 3c. 被积函数在采样点返回非有限值：必须走未收敛报告路径，
#     不得因 inf > inf 为 False 而误判收敛并返回无穷大结果。
r = integrate(lambda x: 1.0 / x if x != 0.0 else math.inf, -1.0, 1.0)
check("非有限值(初始面板)触发未收敛",
      not r.converged and "non-finite" in r.message, f"msg={r.message!r}")


# 3d. 细分后命中内部奇点：x=0.25 恰是子区间 [0, 0.5] 的中心采样点。
def _spike(x):
    return math.inf if x == 0.25 else math.sqrt(x)


r = integrate(_spike, 0.0, 1.0)
check("非有限值(细分后命中)触发未收敛",
      not r.converged and "non-finite" in r.message, f"msg={r.message!r}")

# 3e. max_depth 只冻结到顶的子区间：强尖点 1e6*sqrt(x) 把含 0 的子区间
#     一路推到深度上限并冻结（残留误差 ~7e-3 < epsabs），此时弱尖点
#     1e4*sqrt(1-x) 的更浅子区间仍可继续细分，把总误差压进容差。
#     （旧的“到顶即整体判未收敛”实现在此用例上会返回 converged=False。）
r = integrate(lambda x: 1e6 * math.sqrt(x) + 1e4 * math.sqrt(1.0 - x),
              0.0, 1.0, epsabs=1e-2, epsrel=0.0, max_depth=10)
check("max_depth 冻结到顶区间后整体仍收敛", r.converged, f"msg={r.message!r}")
check("冻结后结果精度达标",
      abs(r.value - (1e6 + 1e4) * 2.0 / 3.0) <= r.error,
      f"value={r.value:.15g} err={r.error:.3g}")

# ---------------------------------------------------------------------------
# 4. 与固定均匀细分方法的求值次数/耗时对比
# ---------------------------------------------------------------------------
print("\n== 4. 自适应 vs 固定均匀细分（GL4 倍增）求值次数对比 ==")

CMP = [
    ("光滑 exp(x)", math.exp, 0.0, 1.0, {}),
    ("振荡 cos(200x)", lambda x: math.cos(200.0 * x), 0.0, 1.0, {}),
    ("近奇点 1/(1+100x^2)", lambda x: 1.0 / (1.0 + 100.0 * x * x), -1.0, 1.0, {}),
    ("端点奇性 x^-1/2", lambda x: x**-0.5, 0.0, 1.0, {"endpoint_transform": True}),
    ("无穷 [0,inf) e^-x", lambda x: math.exp(-x), 0.0, math.inf, {}),
]

print(f"  {'函数':<26} {'自适应neval':>12} {'自适应ms':>9} {'固定neval':>12} {'固定ms':>9} {'倍数':>7}")
for name, f, a, b, kw in CMP:
    ra = integrate(f, a, b, epsabs=1e-9, epsrel=1e-9, **kw)
    rf = fixed_integrate(f, a, b, epsabs=1e-9, epsrel=1e-9)
    ratio = rf.neval / ra.neval if ra.neval else float("nan")
    print(f"  {name:<26} {ra.neval:>12} {ra.elapsed*1e3:>9.3f} "
          f"{rf.neval:>12} {rf.elapsed*1e3:>9.3f} {ratio:>6.1f}x"
          f"{'  (fixed未收敛)' if not rf.converged else ''}")
    check(f"对比用例自适应收敛: {name}", ra.converged)

# ---------------------------------------------------------------------------
print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项 -> {FAILURES}")
    sys.exit(1)
print("ALL TESTS PASSED")
