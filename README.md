# 特性开关求值库（feature_flags）

Python 3，仅标准库。支持默认值、环境覆盖、用户分桶灰度、时间窗口四层配置，
提供一致性快照、规则链可解释输出、冲突确定消解。

## 运行

```bash
python3 selftest.py    # 37 项断言 + 分布数据 + 十万次求值基准
python3 - << 'PY'      # 最小示例
from datetime import datetime, timezone
from feature_flags import FlagStore
store = FlagStore()
store.load([{"key": "checkout_v2", "default": False,
             "env_overrides": {"prod": True},
             "rollouts": [{"rule_id": "r1", "value": "grey", "percentage": 25}],
             "time_window": {"start": "2026-01-01T00:00:00+00:00",
                             "end": "2027-01-01T00:00:00+00:00"}}])
snap = store.snapshot()          # 一次请求抓一个快照
now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
print(snap.evaluate("checkout_v2", user_id="u5", env="staging", now=now).explain())
PY
```

## 优先级（高 → 低，逐层短路，全程留痕）

1. **time_window**：窗口外或窗口非法 → 直接返回 `default`（gate，最高优先级）
2. **env_overrides**：当前环境命中 → 返回覆盖值
3. **rollouts**：按 `SHA-256(salt, flag_key, user_id)` 稳定分桶（万分桶），
   `bucket < percentage * 100` 命中；多条规则按 `priority` 降序取最高层
4. **default**：兜底

每次求值返回 `EvalResult`（value / reason / version / trace），
`result.explain()` 输出完整规则链，例如：

```
flag='checkout_v2' value='grey' reason=rollout (config v1)   # u5 的 bucket=739 < 2500
  [hit     ] time_window: 当前时间在窗口内，继续向下求值
  [miss    ] env_override: env='staging' 无覆盖
  [hit     ] rollout: 规则 'r1' 命中并生效：bucket=739 < 25.0% → 'grey'
```

25% 灰度下并非每个用户都命中：`user_id="u1"` 的 bucket=5372 ≥ 2500，
同一配置求值结果为 `value=False, reason=default`。

## 冲突消解

多条灰度规则同时命中时，**每条命中规则都会在 trace 中留痕**（不会被静默丢弃）：

- 最终生效的规则：`outcome=hit`
- **同优先级**且结论相反的命中：`outcome=conflict`，**按 `rule_id` 字典序取最小者胜出**
  （编译期已排序，消解零成本、跨求值确定一致），同时写 `logging` warning
- 同优先级但结论相同的命中：`outcome=suppressed`（不重复生效，不算冲突）
- 优先级低于胜出层的命中：`outcome=suppressed`（高优先级生效，低优先级仅留痕，不算冲突）

冲突只标记在同优先级规则之间。例如三条规则
`rule_b`(B, p5) / `rule_a`(A, p5) / `rule_z`(Z, p1) 同时命中：

```
flag='f' value='A' reason=rollout (config v1)
  [miss    ] env_override: env=None 无覆盖
  [hit     ] rollout: 规则 'rule_a' 命中并生效：bucket=7067 < 100.0% → 'A'
  [conflict] rollout: 规则 'rule_b' 同样命中：bucket=7067 < 100.0% → 'B'，与同优先级 5 的胜出规则 'rule_a'（'A'）结论冲突，按 rule_id 字典序消解，本规则被压制
  [suppressed] rollout: 规则 'rule_z' 命中：bucket=7067 < 100.0% → 'Z'，但优先级 1 低于胜出优先级 5（'rule_a'），被压制
```

## 边界行为（明确约定）

| 场景 | 行为 |
|---|---|
| 配置缺失 | 返回调用方 `fallback`，reason=`flag_not_found`，不抛异常 |
| percentage = 0 | 永不命中（等价于规则停用） |
| percentage = 100 | 必定命中 |
| percentage 越界 [0,100] | 加载期抛 `ConfigError`，旧配置不受影响 |
| 时间窗口 start > end / 无法解析 | 窗口按"永不生效"处理 → 返回 `default`，reason=`invalid_time_window`，留痕 + warning |
| 裸时间（无时区） | 一律按 UTC 解释 |
| 未传 user_id 但有灰度规则 | 跳过分桶，返回 `default`，trace 记 `skip` |

## 一致性快照

`FlagStore` 采用 **copy-on-write**：`load()` 编译出新配置后持锁整体替换引用；
`snapshot()` 只抓取当前引用与版本号。因此：

- 同一 `Snapshot` 内任意多次求值看到同一份配置（一次请求内开关不会中途变化）；
- 配置更新后旧快照完全不受影响（各自带版本号，可比对）；
- 读路径无锁，并发读取安全（自测含 8 线程读 + 主线程 200 次高频写的压力场景）。

## 缓存 / 预编译策略

- **加载期预编译**：ISO 时间 → epoch 时间戳、灰度规则按 `(priority desc, rule_id asc)`
  预排序、比例校验，求值路径零解析、零排序。
- **分桶记忆化**：`stable_bucket` 经 `functools.lru_cache`（线程安全，上限 100 万条），
  同一 `(flag_key, user_id, salt)` 的 SHA-256 只算一次。

## 实测数据（Python 3.12，本机）

分桶分布（100,000 用户，万分桶）：

| 目标比例 | 实际命中 | 偏差 |
|---|---|---|
| 1% | 1.01% | +0.01pp |
| 10% | 10.08% | +0.08pp |
| 25% | 25.17% | +0.17pp |
| 50% | 50.20% | +0.20pp |
| 75% | 75.31% | +0.31pp |
| 90% | 90.02% | +0.02pp |
| 99% | 99.02% | +0.02pp |
| 100% | 100.00% | +0.00pp |

十个千分区间计数 `[10077, 9981, 10237, 9831, 10072, 10204, 9955, 9809, 9849, 9985]`，
各区偏差 < 3%，分布均匀。

十万次求值耗时（含完整规则链构建）：

- 冷缓存 0.257s（2.57µs/次，约 39 万次/s）；热缓存 0.239s（2.39µs/次，约 42 万次/s）
- 分桶微基准：SHA-256 未命中 0.78µs/次，lru_cache 命中 0.17µs/次，**加速 4.7x**
