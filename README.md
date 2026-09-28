# backend_selector

后端选择器库（纯 Python 3 标准库，无第三方依赖）。提供两种策略、统一接口：

- `SmoothWeightedRoundRobin`：nginx 风格平滑加权轮询
- `LeastConnections`：加权最少连接（score = 在途连接数 / 权重）

## 运行命令

```bash
python3 test_backend_selector.py   # 原有自测（26 个用例）
python3 test_half_open.py          # 半开恢复 / 上界 / 可复现（16 个用例）
python3 demo_distribution.py       # 分布验证数据与偏差
python3 demo_half_open.py          # 半开恢复时间线 + 恢复上界实测（可复跑）
```

## 统一接口

```python
clock = ...                          # 可选：可注入时钟（默认 time.monotonic）
sel = SmoothWeightedRoundRobin(      # 或 LeastConnections()
    cooldown=5.0,                    # 摘除后冷却时长
    required_successes=2,            # 半开期连续成功多少次后完全恢复
    clock=clock)
sel.add_node("a", weight=5)          # 动态增删 / 运行时调权
sel.remove_node("b")
sel.set_weight("a", 3)               # 下一次 select 立即生效，可复现
sel.drain("a")                       # 健康检查失败：摘除进入 OPEN，开始冷却
sel.health_tick()                    # 冷却到期：OPEN -> HALF_OPEN
probe = sel.select_probe()           # 取出一个半开节点做探测（每节点至多1个在途）
sel.report_probe(probe, ok=True)     # 失败重新 OPEN 冷却；连续成功 -> HEALTHY
sel.run_health_checks(probe_fn)      # 上面三步合一：一轮完整健康检查
sel.restore("a")                     # （可选）人工强制直接恢复
nid = sel.select()                   # 只选 HEALTHY 且 weight>0 的节点
sel.release(nid)
sel.state_of("a")                    # HEALTHY / OPEN / HALF_OPEN
sel.recovery_bound("a")              # 恢复后重新参与的选择次数上界
sel.stats()                          # 各节点次数 / 占比 / 状态
```

## 健康状态机（半开恢复）

每个节点处于且仅处于三种状态之一：

- **HEALTHY**：正常承接流量。
- **OPEN（已摘除，冷却中）**：健康检查失败后进入。冷却期内
  `select()` 与 `select_probe()` 都不会选到它；**生产流量绝不打到摘除节点**
  （测试中对每一次 select 断言）。
- **HALF_OPEN（半开，仅探测）**：冷却到期后进入。选择器**每节点至多放出一个
  探测请求**（`probe_in_flight` 门控，不同节点互不影响）：
  - 探测失败 -> 回到 OPEN，冷却重新计时，成功计数清零；
  - 探测成功且未满 `required_successes` -> 保持 HALF_OPEN，等待下一次探测；
  - **连续**成功达到 `required_successes` -> 完全恢复 HEALTHY（成功必须连续，
    中途失败立即清零）。
  - 半开节点**绝不参与正常 `select()`**，因此恢复期间最多暴露一个探测请求。

所有节点 OPEN/HALF_OPEN 或权重全为 0 时，`select()` 抛出
`NoBackendAvailableError`（不会拿探测节点静默顶上生产流量）。

## 语义约定

- **可参与条件**：状态为 HEALTHY 且权重 > 0。OPEN / HALF_OPEN / 权重 0 一律
  不参与正常选择；权重 0 的节点冷却到期也不会收到探测请求。
- **运行时调权**：`set_weight` 对下一次选择立即生效；调权不改变健康状态，
  权重归零立即退出，调回正值即按其当前状态参与。
- **确定性 / 可复现**：注入相同时间值的时钟后，给定相同操作序列
  （增删/调权/摘除/tick/探测/上报/选择/释放），选择序列逐位一致。并列决胜按
  节点首次加入顺序（插入序）；完全恢复的节点在恢复瞬间拥有一次性并列优先权。
- **线程安全**：所有公开方法由同一把锁保护。

## 恢复上界（「恢复后最多多少次选择内重新参与」）

从节点**完全恢复（HEALTHY）的那一次 select 起算**，且恢复窗口内权重不变：

- **SmoothWeightedRoundRobin**：恢复节点权重 w、全部 weight>0 节点总权重 T 时，
  **至多 `ceil(T / w)` 次选择内必被选中**。恢复瞬间其 current_weight 被重置为
  0，而其余节点在它摘除期间以总权重 T−w 独立运行、current_weight 上界为 T−w；
  恢复后其 current_weight 每轮增加 w，故第 k 轮满足 k·w > T−w 时必胜，即
  k ≥ ceil(T/w)。运行时把权重调高只会更早被选中；`recovery_bound()` 返回按
  当前权重计算的值。
- **LeastConnections**：恢复后在途连接清零（score = 0，全局最小），并对所有
  同为 0 分的节点拥有一次性并列优先权，因此**下一次选择必中，上界 = 1**
  （即使其他节点的连接在此期间被全部释放也成立）。

`test_half_open.py` 对 SmoothWRR 在 7 组权重配置 × 每个节点 × 0..3T 种摘除
时长下穷举验证该上界；`demo_half_open.py` 打印逐节点实测（observed ≤ bound）。

## 分布验证数据（`demo_distribution.py` 实测输出）

| 场景 | 选择次数 | 结果 | 最大偏差 |
|---|---|---|---|
| 权重 5:3:2 | 10000 | 5000 / 3000 / 2000 | 0.000000 |
| 权重 7:3:1 | 11000 | 7000 / 3000 / 1000 | 0.000000 |
| 权重 1000:1（极端悬殊） | 10010 | 10000 / 10 | 0.000000 |
| 权重 5:3:2，非整轮（999 次） | 999 | 499 / 300 / 200 | 0.000501 |

平滑 WRR 在总权重整数倍的选择次数下分布与权重比例**完全一致**（偏差为 0）；
非整轮时偏差不超过 1 次选择对应的份额。

## 测试覆盖

- `test_backend_selector.py`（26 个用例）：分布精确性、平滑性、确定性重放、
  插入序决胜、摘除期间零选中、强制恢复上界、权重 0 不参与、全不可用抛错、
  动态增删与调权、8 线程并发完整性。
- `test_half_open.py`（16 个用例）：
  - 摘除/半开期间逐次 select 断言不命中；全摘除时 `select()` 抛错
  - 冷却边界（到期前无探测、到期恰好可探测）、每节点单探测门控
  - 探测失败重新冷却；成功必须**连续**达到阈值（失败清零）
  - 多节点半开：插入序取探测、失败节点重新冷却
  - 权重 0：冷却到期也不探测、恢复后权重 0 仍不参与
  - SmoothWRR 上界穷举验证、`recovery_bound()` 取值、LC 上界 = 1
  - 注入时钟下两种策略的操作脚本两次重放逐位一致；运行时调权后分布可复现
