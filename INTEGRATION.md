# 自适应数值积分库

- `adaptive_integrate.py` — 库（仅标准库）：Gauss7-Kronrod15 自适应细分。
- `test_adaptive_integrate.py` — 自测与基准数据。

## 运行

```bash
python3 test_adaptive_integrate.py   # 退出码 0 = 全部检查通过
```

## 用法

```python
from adaptive_integrate import integrate
r = integrate(f, a, b, atol=1e-10, rtol=1e-10,
              max_depth=30, max_neval=100_000, max_intervals=10_000,
              endpoint_transform=False)
r.value, r.error, r.converged, r.neval, r.nintervals, r.elapsed
r.uncertainty   # (value-error, value+error)
```

## 设计要点

- **误差估计**：Kronrod 与 Gauss 结果之差，经 QUADPACK 风格 resasc 修正
  与舍入下界保护；自测要求所有解析对拍用例满足 实际误差 ≤ 报告误差。
- **端点奇性**：开公式从不采样端点，可积端点奇性（1/sqrt(x)、log x）直接积分。
  可选 `endpoint_transform=True` 启用 sin² 端点压缩映射
  （x = a + (b-a)·sin²(πt/2)），把求值节点向端点二次压缩，
  代数/对数端点奇性被 Jacobian 抵消：1/sqrt(x) 在 1e-10 容差下
  由直接积分的未收敛（约 10 万次求值）降为 15 次求值收敛；
  报告误差仍是可验证上界（自测逐项检查 实际误差 ≤ 报告误差）。
- **区间反序**：自动交换端点并对结果取负（非报错）。
- **无穷端点**：支持 `[a,+inf)`、`(-inf,b]`（变量替换 `x = a + (1-t)/t`）
  与 `(-inf,+inf)`（在 0 处拆分，容差对半）。
- **未收敛**：达到 max_depth / max_neval / max_intervals 或被积函数出现
  非有限值时，返回 `converged=False`、当前估计与不确定性区间，绝不静默成功。
