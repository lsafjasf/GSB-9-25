# fairshare — 加权公平分配库

纯 Python 3 标准库实现，无第三方依赖。

## 模型

n 个租户共享容量 `capacity`，租户 i 有权重 `w_i`、申请量 `d_i`、最小保障 `m_i`。
先发放保障，剩余容量按权重注水法（water-filling）分配，每人上限为申请量。

## 不变量（可行时严格成立，测试中断言）

- 守恒：`sum(alloc) == min(capacity, 有权租户申请之和 + 零权重租户保障之和)`
- 上限：`alloc_i <= d_i`；下限：`alloc_i >= m_i`
- 零权重：`w_i == 0 => alloc_i == 0`（故零权重租户保障必须为零，否则报不可行）
- 确定性：Fraction 精确水位 + 固定余数规则（小数部分降序、索引升序），同输入结果一致

## 不可行处理

`sum(m_i) > capacity`（或 `m_i > d_i`、零权重带保障）时抛出 `InfeasibleError`，
异常携带 `deficit`（缺口）与 `conflicts`（冲突方列表），绝不静默削减保障。

## 复杂度

- 时间：O(n log n)——按 `剩余额度/权重` 排序主导；注水扫描与余数分发均为 O(n)
- 空间：O(n)

## 运行

```bash
python3 -m unittest -v     # 不变量断言测试 + 边界 + 不可行用例（13 例）
python3 benchmark.py       # 1 万租户规模基准
```

实测（Python 3.12，普通服务器）：10,000 租户单次分配约 **35–42 ms**。
