"""边界样例对照：重复横坐标 / 非等距 / 极端外推 / 自定义核。

同时验证：批量接口 batch/upsample 的结果与逐点调用逐元素完全相等
（同一浮点值，用 is + == 双重确认，不是近似）。

运行：python3 examples/boundary_demo.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interp import (
    Interpolator,
    Kernel,
    available_kernels,
    register_kernel,
    upsample,
)


class NearestKernel(Kernel):
    """自定义核示例：最近邻（区间中点归右端点）。"""

    name = "nearest"

    def build(self, xs, ys):
        self.xs, self.ys = xs, ys

    def eval_segment(self, i, x):
        xs, ys = self.xs, self.ys
        return ys[i] if x - xs[i] < xs[i + 1] - x else ys[i + 1]


def check_identity(f, grid, title):
    """批量 vs 逐点：逐元素完全相等（先比对象同一性，再比值）。"""
    batch = f.batch(grid)
    pointwise = [f(x) for x in grid]
    same = len(batch) == len(pointwise) and all(
        a is b or a == b for a, b in zip(batch, pointwise))
    print(f"  批量 vs 逐点逐元素一致: {'PASS' if same else 'FAIL'}  ({title})")
    return batch, same


def main():
    print("=" * 72)
    print("可用核:", available_kernels())
    register_kernel("nearest", NearestKernel)
    print("注册自定义核后:", available_kernels())

    # ---- 1. 重复横坐标：error / last / first -------------------------
    print("\n[1] 重复横坐标 x=1，原始 y 依次为 1、9（duplicates 策略对照）")
    xs, ys = [0, 1, 1, 2], [0, 1, 9, 4]
    grid = [0.0, 0.5, 1.0, 1.5, 2.0]
    try:
        Interpolator(xs, ys, kernel="linear")
    except ValueError as e:
        print(f"  error（默认）      : ValueError -> {e}")
    f_last = Interpolator(xs, ys, kernel="linear", duplicates="last")
    f_first = Interpolator(xs, ys, kernel="linear", duplicates="first")
    print(f"  last : 节点 {f_last.xs} -> 曲线 {f_last.batch(grid)}")
    print(f"  first: 节点 {f_first.xs} -> 曲线 {f_first.batch(grid)}")
    check_identity(f_last, grid, "重复横坐标")

    # ---- 2. 非等距采样：两种内置核 + 自定义核 ------------------------
    print("\n[2] 非等距采样（密区间 0~0.5 + 疏区间 0.5~5），三核批量曲线")
    xs = [0.0, 0.01, 0.5, 5.0]
    ys = [0.0, 2.0, -1.0, 4.0]
    grid = [0.0, 0.005, 0.01, 0.25, 0.5, 2.75, 5.0]
    for name in ("linear", "pchip", "nearest"):
        f = Interpolator(xs, ys, kernel=name)
        vals = f.batch(grid)
        print(f"  kernel={name:<7}: " +
              "  ".join(f"{v:8.4f}" for v in vals))
        check_identity(f, grid, f"kernel={name}")

    # ---- 3. 极端外推：clamp / linear / reject ------------------------
    print("\n[3] 极端外推（目标点 +/-1e9）：clamp 有界 vs linear 无界")
    xs, ys = [0.0, 1.0, 2.0], [0.0, 2.0, 3.0]
    grid = [-1e9, -1.0, 0.0, 2.0, 3.0, 1e9]
    f_clamp = Interpolator(xs, ys, kernel="pchip", extrapolate="clamp")
    f_lin = Interpolator(xs, ys, kernel="pchip", extrapolate="linear")
    print(f"  clamp : {f_clamp.batch(grid)}")
    print(f"  linear: {[f'{v:.4g}' for v in f_lin.batch(grid)]}")
    check_identity(f_lin, grid, "线性外推")
    f_rej = Interpolator(xs, ys, kernel="pchip", extrapolate="reject")
    try:
        f_rej.batch([0.5, 3.0])
    except ValueError as e:
        print(f"  reject: ValueError -> {e}")

    # ---- 4. upsample 一把梭 + 乱序目标网格 ---------------------------
    print("\n[4] upsample 一次插整条曲线（目标网格乱序、含外推、含重复位置）")
    target = [5.0, 0.0, -1e6, 2.75, 0.01, 0.01, 1e6]
    out = upsample(xs, ys, target, kernel="pchip", extrapolate="linear")
    ref = [Interpolator(xs, ys, extrapolate="linear")(x) for x in target]
    print(f"  grid : {target}")
    print(f"  out  : {[f'{v:.6g}' for v in out]}")
    print(f"  批量 vs 逐点逐元素一致: {'PASS' if out == ref else 'FAIL'}")

    # ---- 5. 非法参数的明确报错 ---------------------------------------
    print("\n[5] 非法参数错误对照")
    cases = [
        ("未知核名", lambda: upsample(xs, ys, [0.5], kernel="cubic")),
        ("目标网格是标量", lambda: upsample(xs, ys, 0.5)),
        ("网格含 NaN", lambda: upsample(xs, ys, [0.5, float('nan')])),
        ("采样含 inf", lambda: upsample([0, 1], [0, float('inf')], [0.5])),
        ("xs/ys 不等长", lambda: upsample([0, 1], [0], [0.5])),
        ("空采样", lambda: upsample([], [], [0.5])),
        ("非数值元素", lambda: upsample(xs, ys, [0.5, "x"])),
    ]
    for title, fn in cases:
        try:
            fn()
        except (ValueError, TypeError) as e:
            print(f"  {title:<14}: {type(e).__name__}: {e}")

    print("\n" + "=" * 72)
    print("说明：batch/upsample 与逐点调用共享同一求值路径，")
    print("      上述所有 PASS 均为逐元素同一浮点值（非容差近似）。")


if __name__ == "__main__":
    main()
