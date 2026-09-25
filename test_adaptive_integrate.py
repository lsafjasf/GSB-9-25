"""
test_adaptive_integrate.py -- adaptive_integrate 的自测与基准数据

运行：python3 test_adaptive_integrate.py
退出码：全部检查通过为 0，否则为 1。
"""

import math
import sys
import time

from adaptive_integrate import integrate, fixed_simpson

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    if not ok:
        FAILURES.append(name)
    print(f"  [{tag}] {name}" + (f"  {detail}" if detail else ""))


def hr(title: str) -> None:
    print()
    print("=" * 96)
    print(title)
    print("=" * 96)


# ---------------------------------------------------------------------------
# 1. 解析对拍：报告的误差估计必须 >= 实际误差
# ---------------------------------------------------------------------------
hr("1. 解析对拍：|估计值-精确值| <= 报告的误差估计")

SQRT_PI = math.sqrt(math.pi)
CASES = [
    # (名称, f, a, b, 精确值, atol, rtol)
    ("exp(x)        [0,1]",        math.exp,                      0.0, 1.0, math.e - 1.0,        1e-12, 1e-12),
    ("x^5-2x^3+x    [0,2]",        lambda x: x**5 - 2*x**3 + x,   0.0, 2.0, 14.0/3.0,            1e-12, 1e-12),
    ("1/(1+x^2)     [0,1]",        lambda x: 1.0/(1.0+x*x),       0.0, 1.0, math.pi/4.0,         1e-12, 1e-12),
    ("sqrt(x)       [0,1]",        math.sqrt,                     0.0, 1.0, 2.0/3.0,             1e-10, 1e-10),
    ("1/sqrt(x)     [0,1] 端点奇性", lambda x: 1.0/math.sqrt(x),  0.0, 1.0, 2.0,                 1e-4,  1e-4),
    ("-log(x)       [0,1] 端点奇性", lambda x: -math.log(x),      0.0, 1.0, 1.0,                 1e-6,  1e-6),
    ("sin(100x)     [0,1] 振荡",   lambda x: math.sin(100.0*x),   0.0, 1.0, (1.0-math.cos(100.0))/100.0, 1e-12, 1e-12),
    ("sin(16*pi*x)  [0,1] 振荡",   lambda x: math.sin(16.0*math.pi*x), 0.0, 1.0, 0.0,            1e-12, 1e-12),
    ("exp(-x)       [0,inf)",      lambda x: math.exp(-x),       0.0, math.inf, 1.0,           1e-10, 1e-10),
    ("1/(1+x^2)     [0,inf)",      lambda x: 1.0/(1.0+x*x),       0.0, math.inf, math.pi/2.0,    1e-10, 1e-10),
    ("exp(-x^2)     (-inf,inf)",   lambda x: math.exp(-x*x),      -math.inf, math.inf, SQRT_PI,  1e-10, 1e-10),
    ("1/(1+x^2)     (-inf,inf)",   lambda x: 1.0/(1.0+x*x),       -math.inf, math.inf, math.pi,  1e-10, 1e-10),
]

print(f"  {'函数/区间':<30} {'精确值':>18} {'估计值':>18} {'报告误差':>12} {'实际误差':>12} {'实际/报告':>10} {'求值':>6} {'子区间':>6}")
for name, f, a, b, exact, atol, rtol in CASES:
    r = integrate(f, a, b, atol=atol, rtol=rtol)
    actual = abs(r.value - exact)
    ratio = actual / r.error if r.error > 0 else (0.0 if actual == 0 else math.inf)
    ok = r.converged and actual <= r.error
    print(f"  {name:<30} {exact:>18.12g} {r.value:>18.12g} {r.error:>12.3e} {actual:>12.3e} {ratio:>10.3f} {r.neval:>6} {r.nintervals:>6}")
    check(f"对拍 {name.strip()}", ok,
          f"actual={actual:.3e} reported={r.error:.3e} converged={r.converged}")

# ---------------------------------------------------------------------------
# 2. 未收敛用例：必须显式报告，不得静默成功
# ---------------------------------------------------------------------------
hr("2. 未收敛用例：显式报告 converged=False 与不确定性区间")

r = integrate(lambda x: 1.0/math.sqrt(x), 0.0, 1.0, atol=1e-12, rtol=1e-12, max_depth=2)
check("max_depth 限制 -> 未收敛", not r.converged,
      f"value={r.value:.6f} err={r.error:.3e} 区间=({r.uncertainty[0]:.6f},{r.uncertainty[1]:.6f}) msg={r.message!r}")

r = integrate(lambda x: math.sin(1.0/x) if x != 0.0 else 0.0, 0.0, 1.0,
              atol=1e-12, rtol=1e-12, max_neval=450)
check("max_neval 限制 -> 未收敛", not r.converged,
      f"value={r.value:.6f} err={r.error:.3e} neval={r.neval} msg={r.message!r}")

r = integrate(lambda x: 1.0/(x-0.5)**2, 0.0, 1.0, max_neval=10_000)
check("内部奇点(发散积分) -> 未收敛而非静默错值", not r.converged,
      f"msg={r.message!r}")

r = integrate(lambda x: 1.0/math.sqrt(x), 0.0, 1.0, atol=1e-12, rtol=1e-12, max_depth=2)
lo, hi = r.uncertainty
check("未收敛仍返回估计与不确定性区间", not r.converged and lo < r.value < hi and r.error > 0)

# ---------------------------------------------------------------------------
# 3. 区间反序：自动交换端点并取负
# ---------------------------------------------------------------------------
hr("3. 区间反序（自动纠正：交换端点、结果取负）")

r_fwd = integrate(math.exp, 0.0, 1.0)
r_rev = integrate(math.exp, 1.0, 0.0)
check("integrate(f,1,0) == -integrate(f,0,1)",
      abs(r_fwd.value + r_rev.value) <= 1e-12 and r_rev.converged,
      f"fwd={r_fwd.value:.15g} rev={r_rev.value:.15g}")

# ---------------------------------------------------------------------------
# 4. 求值次数与耗时：自适应 vs 等距固定细分 Simpson(n=1024)
# ---------------------------------------------------------------------------
hr("4. 求值次数 / 耗时对比：自适应 GK15 vs 固定细分 Simpson(1024 子区间, 1025 次求值)")

print(f"  {'函数/区间':<30} {'自适应求值':>10} {'固定求值':>8} {'倍数':>8} {'自适应ms':>10} {'固定ms':>10} {'固定实际误差':>12}")
for name, f, a, b, exact, atol, rtol in CASES:
    if math.isinf(a) or math.isinf(b):
        continue  # 固定 Simpson 不支持无穷区间
    r = integrate(f, a, b, atol=atol, rtol=rtol)
    t0 = time.perf_counter()
    try:
        v_fix, n_fix = fixed_simpson(f, a, b, 1024)
        err_fix_s = f"{abs(v_fix - exact):>12.3e}"
        n_fix_s, t_fix_s = f"{n_fix:>8}", f"{(time.perf_counter()-t0)*1e3:>10.3f}"
        ratio_s = f"{n_fix / r.neval:>8.2f}"
    except (ZeroDivisionError, ValueError, OverflowError):
        err_fix_s = f"{'n/a':>12}"
        n_fix_s, t_fix_s, ratio_s = f"{'n/a':>8}", f"{'n/a':>10}", f"{'n/a':>8}"
    print(f"  {name:<30} {r.neval:>10} {n_fix_s} {ratio_s} {r.elapsed*1e3:>10.3f} {t_fix_s} {err_fix_s}")

# ---------------------------------------------------------------------------
hr("汇总")
if FAILURES:
    print(f"  {len(FAILURES)} 项检查失败: {FAILURES}")
    sys.exit(1)
print("  全部检查通过。")
