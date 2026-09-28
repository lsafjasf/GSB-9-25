# interp — 保形插值库（Python 3，仅标准库）

把离散采样补成密集序列时，普通高阶插值（如未限幅的三次样条）会在跳变处
过冲，产生物理上不可能的值。本库提供**可插拔核**的统一插值器，内置两种
核均保证**零过冲**且**保持单调性**：

- `"linear"` / `LinearKernel`：分段线性。
- `"pchip"` / `MonotoneCubicKernel`：Fritsch-Carlson 保形分段三次 Hermite
  插值（PCHIP），曲线光滑（C1）且不引入新的极值。

核可插拔：传入内置名、`Kernel` 子类/实例，或 `register_kernel` 注册自定义核。

## 用法：逐点 与 批量（一次插整条曲线）

```python
from interp import Interpolator, upsample

# 逐点
f = Interpolator([0, 1, 2, 3], [0, 0, 1, 1], kernel="pchip")
y = f(1.5)

# 批量：原始采样 + 目标网格 -> 整条曲线（等价的两种写法）
curve = f.batch([0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0])
curve = upsample([0, 1, 2, 3], [0, 0, 1, 1],
                 [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0], kernel="pchip")
```

输入不必排序、不必等距，库内先按 x 升序排序。目标网格也不要求排序，
可含内点、端点、外推点和重复位置；空网格返回空 `list`。

**批量一致性**：`batch`/`upsample` 对每个网格点走与 `f(x)` 完全相同的
内部求值路径（同一份浮点运算），因此
`f.batch(g) == [f(x) for x in g]` 是**逐元素同一浮点值**（位级一致，
非容差近似）。该性质由 120 组随机用例 × 3 种核 × 3 种外推策略断言覆盖，
并另有 5 万+元素的位级（double 二进制表示）比对。

## 自定义核

继承 `interp.Kernel`，实现 `eval_segment(i, x)`（区间内求值）；按需覆盖
`build(xs, ys)`（预计算）与 `slope_lo/slope_hi`（线性外推斜率）：

```python
from interp import Interpolator, Kernel, register_kernel

class NearestKernel(Kernel):
    name = "nearest"
    def build(self, xs, ys):
        self.xs, self.ys = xs, ys
    def eval_segment(self, i, x):
        xs, ys = self.xs, self.ys
        return ys[i] if x - xs[i] < xs[i+1] - x else ys[i+1]

Interpolator(xs, ys, kernel=NearestKernel)   # 直接传类/实例
register_kernel("nearest", NearestKernel)    # 或注册后按名字取用
Interpolator(xs, ys, kernel="nearest")
```

`available_kernels()` 返回当前可用核名（`linear`、`pchip`、
`monotone_cubic`（pchip 别名）及已注册自定义核）。

## 重复横坐标规则（`duplicates` 参数）

| 取值      | 行为                                   |
|-----------|----------------------------------------|
| `"error"` | **默认**。发现重复 x 抛 `ValueError`   |
| `"last"`  | 去重，同一 x 取最后出现的 y            |
| `"first"` | 去重，同一 x 取最先出现的 y            |

## 外推策略（`extrapolate` 参数）

| 取值      | 行为             | 极端延伸（|x|→∞）表现          |
|-----------|------------------|-------------------------------|
| `"clamp"` | **默认**。保持端点值 | 恒为端点值，有界，绝不产生新极值 |
| `"reject"`| 越界抛 `ValueError` | 直接报错，最保守               |
| `"linear"`| 按端点切线斜率外推 | 线性发散、无界，可能越出采样值范围 |

单点输入视为常数函数，任何策略下任意 x 都返回该点的 y。

## 错误约定

- `ValueError`：数据语义非法——空采样、`xs/ys` 不等长、含 NaN/inf、
  重复横坐标（`duplicates="error"`）、越界（`extrapolate="reject"`）、
  未知核名、非法的 `duplicates`/`extrapolate` 取值。
- `TypeError`：类型非法——核不是名字/`Kernel` 子类/实例、采样或目标
  网格含非数值元素、把标量传给 `batch`/`upsample`（标量请用 `f(x)`，
  序列才走批量）。

错误信息均带具体取值与可选范围，例如：
`ValueError: 未知核 'cubic'；可用核: ['linear', 'monotone_cubic', 'pchip']...`。

## 性质保证

对任意（含非等距、跨数量级横坐标的）输入：

1. **零过冲**：每个区间内插值不越出该区间两端采样点的范围；
2. **保单调**：采样点单调（非降/非升）时，插值结果同样单调。

两条性质均由随机数据断言覆盖（200 组随机用例 × 两种插值器）。

## 过冲对照数据

`python3 test_interp.py` 输出对照表（越出相邻采样点范围的最大距离）：

```
用例                    线性        保形三次        普通三次
阶跃             0.000e+00   0.000e+00   7.405e-02
锯齿             0.000e+00   0.000e+00   0.000e+00
单调台阶           0.000e+00   0.000e+00   7.405e-02
非等距阶跃          0.000e+00   0.000e+00   1.479e-01
```

"普通三次"为测试内未限幅的 Catmull-Rom 样条，仅作对照：阶跃处过冲最高
达采样间距的 ~15%，而本库两种插值器过冲恒为 0。

## 运行

```bash
python3 test_interp.py                 # 全部 30 个测试 + 过冲对照表
python3 test_interp.py -v     # 逐条用例输出
python3 examples/boundary_demo.py     # 边界样例对照（真实输出可复跑）
```

## 文件

- `interp/__init__.py` — `Interpolator` 统一插值器、`upsample` 批量接口、
  核注册表、排序/去重/外推/参数校验、两个向后兼容类
- `interp/_kernels.py` — `Kernel` 基类与内置 `LinearKernel`、
  `MonotoneCubicKernel`（PCHIP）
- `test_interp.py` — 性质断言、核可插拔、批量逐元素一致性（随机 120 组 ×
  3 核 × 3 外推）、`upsample`、重复横坐标、外推、过冲对照、非法参数、
  边界用例（单点 / 两点 / 全部相同 / 5000 密点 / 跨 12 个数量级）
- `examples/boundary_demo.py` — 边界样例对照：重复横坐标（error/last/first）、
  非等距（三核曲线对照）、极端外推（clamp/linear/reject）、乱序批量、
  自定义核、非法参数报错；每节打印"批量 vs 逐点"逐元素一致 PASS

## 兼容性

旧 API 完全保留：`LinearInterpolator`/`MonotoneCubicInterpolator` 仍是
`Interpolator` 的薄封装（分别锁定 `kernel="linear"`/`"pchip"`），构造参数、
`xs`/`ys` 属性、逐点调用、`_slope_lo/_slope_hi` 均不变；默认核为 `pchip`。
