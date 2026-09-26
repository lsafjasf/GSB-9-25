# backend_selector

纯标准库（Python 3.8+）的确定性后端选择器：加权轮询 + 加权最少连接，统一接口。

## 运行

```bash
cd backend_selector
python3 -m unittest -v     # 自测（20 个用例）
python3 demo.py            # 分布验证数据
```

## 接口（两种策略一致）

| 方法 | 语义 |
|---|---|
| `add(id, weight)` / `remove(id)` | 动态增删节点 |
| `set_weight(id, w)` | 调整权重（`w=0` 永久不参与选择） |
| `set_alive(id, bool)` | 健康检查摘除 / 恢复 |
| `select() -> id` | 选择节点；无可用节点抛 `NoAvailableBackendError` |
| `stats()` | `{id: (次数, 占比)}` |
| `release(id)` / `connection()` | 仅 `LeastConnections`：释放在途连接 / 上下文管理器 |

## 核心语义

- **参与条件**：`alive and weight > 0`，否则永不被选中；全部不可用时 `select()` 抛错。
- **确定性**：相同的操作序列（add/remove/set_weight/set_alive/select/release）产生完全
  相同的选择序列，跨进程、跨运行可重现。
- **并列决胜**：策略键相同者，取 id 最小（id 类型的自然序）的节点。
  - WRR 键：平滑轮询的当前权重（`-current`）。
  - LC 键：`in_flight / weight`（权重大的节点应承载更多连接）。
- **恢复上界（WRR）**：权重为 `w` 的节点恢复后，总有效权重为 `W`，至多
  `ceil(W / w)` 次 `select()` 内必被再次选中（SWRR 性质，见源码 docstring 证明）。
- **并发安全**：所有状态变更在锁内完成；并发选择不丢不重（有测试覆盖）。

## 分布验证数据（python3 demo.py 实测）

```
== WeightedRoundRobin 5:3:2 (n=100000) ==
node     weight  expected    actual       dev
a             5    0.5000    0.5000  0.000000
b             3    0.3000    0.3000  0.000000
c             2    0.2000    0.2000  0.000000

== WeightedRoundRobin 10000:1 (n=100000) ==
heavy       10000    0.9999    0.9999  0.000000
light           1    0.0001    0.0001  0.000000

== LeastConnections 权重 1:1:2，16 个持有连接 ==
序列: x y z z x y z z x y z z x y z z
在途: x=4, y=4, z=8   (精确 1:1:2)
```

SWRR 每个周期（长度 = 权重和）内分布精确，残差偏差 ≤ W/n。

## 测试覆盖（test_backend_selector.py）

- 单节点、权重为零不参与、全部摘除抛 `NoAvailableBackendError`
- 摘除后不再被选中；恢复后在 `ceil(W/w)` 上界内重新参与（含 10000:1 极端权重）
- 分布与权重比例相符（最大偏差断言 < 0.001，实测为 0）
- 确定性：相同操作序列两次执行结果逐位一致；并列取最小 id
- 动态增删节点、运行中调权
- 并发：8 线程 × 5000 次选择，总数不丢不重、分布仍符合权重；
  LC 并发 select/release 后在途归零
- LC：在途连接变化即时反映到后续选择；摘除节点恢复后（在途为 0）立即优先被选中
