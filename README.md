# feature_flags — 分层特性开关求值库

纯 Python 3 标准库实现，零依赖。同一开关按 **用户分桶灰度、环境、时间窗口** 分层配置；
通过 **一致性快照** 保证一次请求内开关不中途变化。

## 文件

- `feature_flags.py` — 库源码（编译、求值、快照、存储）
- `selftest.py` — 自测 + 分桶分布统计 + 十万次求值基准
- `demo_migration.py` — 跨环境导出→导入→版本对比→阶梯放量端到端演示
- `selftest_output.log` / `demo_output.log` — 上述两个脚本的真实运行输出

## 运行

```bash
python3 selftest.py        # 运行全部 53 项自测与基准
python3 demo_migration.py  # 跨环境搬运 + 版本对比 + 阶梯灰度演示
python3 -c "import feature_flags"   # 仅导入库
```

## 配置格式

```python
config = {
    "salt": "my-app",               # 分桶盐，隔离不同系统的分桶结果
    "flags": {
        "new_checkout": {
            "default": False,                       # 第 3 层：默认值
            "env": {"staging": True},               # 第 2 层：环境覆盖
            "rules": [                              # 第 1 层：规则列表
                {"id": "gray-25", "value": True, "percentage": 25,
                 "window": {"start": "2026-09-01T00:00:00+08:00",
                            "end":   "2026-10-01T00:00:00+08:00"}},
            ],
        },
    },
}
```

## 优先级（高 → 低）

1. **rules 规则列表**：按定义顺序逐条匹配。规则需同时满足：时间窗口有效且
   当前时刻在窗口内、`percentage` 分桶命中。先命中的规则胜出。
2. **env 环境覆盖**：当前环境名命中时取固定值。
3. **default 默认值**。
4. **开关缺失**：返回调用方传入的 `fallback`，轨迹留痕。

每次求值返回 `Evaluation(value, layer, trace, conflicts)`，`trace` 记录完整
规则链（哪条规则命中/跳过及原因），`explain()` 输出人类可读解释。

## 分桶

- 算法：`sha1(f"{salt}:{flag}:{user_id}")` 取前 8 字节对 10000 取模 →
  万分桶 `[0, 10000)`，`bucket < percentage * 100` 即命中，灰度精度 0.01%。
- 稳定性：同一 `(salt, flag, user_id)` 永远得到同一分桶，与进程、实例无关
  （自测中跨实例 500 个用户结果完全一致）。
- 均匀性实测（10 万个用户，本机运行）：

| 目标比例 | 实际命中 | 偏差     |
|---------:|---------:|---------:|
| 1%       | 1.02%    | +0.02 pp |
| 10%      | 10.08%   | +0.08 pp |
| 25%      | 25.01%   | +0.01 pp |
| 50%      | 50.10%   | +0.10 pp |
| 75%      | 75.09%   | +0.09 pp |
| 90%      | 89.90%   | -0.10 pp |
| 99%      | 99.02%   | +0.02 pp |

各比例偏差均 < 0.1 个百分点。

## 一致性快照

```python
store = FlagStore(config, env="prod")
snap = store.snapshot()          # 固定配置引用 + 固定 now
snap.is_enabled("new_checkout", "u-1")   # 多次求值结果一致
store.update(new_config)         # 原子替换；snap 仍看到旧配置，不受影响
```

- 快照持有 **编译后不可变配置** 的引用（创建后不再修改），`update` 整体替换
  引用而非原地修改，因此旧快照天然隔离。
- 并发读取无需加锁：读引用是原子操作，配置不可变。自测中 8 读线程 + 1 写
  线程运行 1s 无不一致读取。
- 快照同时固定 `now`：同一快照内多次求值不会跨越时间窗口边界而自相矛盾。

## 冲突与边界行为

| 场景 | 行为 |
|---|---|
| 同优先级规则相反结论 | 先定义者胜（确定性），被压制规则记入 `conflicts` |
| 开关未配置 | 返回调用方 `fallback`，`layer="fallback"`，轨迹留痕 |
| `percentage=0` | 永不命中（短路，不计算哈希） |
| `percentage=100` | 恒命中（短路，无需 `user_id`） |
| `percentage` 越界 | 编译期抛 `ValueError`（明确失败） |
| `rollout` 比例越界 / 时间不递增 / 比例回退 | 编译期抛 `ValueError`（阶梯配置被拒绝） |
| 规则同时声明 `percentage` 与 `rollout` | 编译期抛 `ValueError` |
| 规则集包 `format` 不支持 / JSON 非法 / 编译不过 | 导入期抛 `ValueError`（拒绝上线） |
| 非法时间窗口（start ≥ end） | 规则永不生效，编译期记入 `warnings`，求值轨迹留痕 |
| 灰度规则但无 `user_id` | 灰度规则视为未命中，回落下一层 |

## 规则集导入导出（跨环境搬运）

```python
from feature_flags import export_bundle, export_json, import_bundle

bundle = export_bundle(config, version="v2", source_env="staging")  # dict
payload = export_json(config, version="v2", source_env="staging")   # JSON 字符串
config2, meta = import_bundle(payload)   # 接受 JSON 字符串/bytes/dict/裸 config
store.update(config2)                    # 原子上线
```

- 包格式：`{"format": "feature-flags/ruleset/v1", "config": {...},
  "meta": {"version", "source_env", "exported_at", "flag_count"}}`。
- **导出前先编译校验**，坏配置不出门；**导入时再次编译**，坏配置不进门：
  越界比例、非法阶梯、坏 JSON、不支持的 `format` 一律抛 `ValueError`。
- JSON 往返无损：`import_bundle(export_json(cfg))[0] == cfg`（自测断言）。

## 版本差异对比

`diff_configs(old, new)` 逐条列出两版本差异，接受裸 config 或导出包：

```
新增（2）：
  + flag 'dark_mode'：0 条规则，default=False
  + flag 'new_checkout' 规则 'vip-early'：5%
删除（1）：
  - flag 'legacy_pay'：0 条规则，default=True
参数变化（5）：
  ~ flag 'new_checkout' [rule:gray] order: 0 -> 1
  ~ flag 'new_checkout' [rule:gray] percentage: 10% -> （无，使用 rollout 阶梯）
  ~ flag 'new_checkout' [rule:gray] rollout: （无） -> [2026-10-01T00:00:00Z@0%, ...]
  ~ flag 'new_checkout' [rule:gray] window_end: '2026-12-31T23:59:59Z' -> None
  ~ flag 'new_checkout' [rule:gray] window_start: '2026-09-01T00:00:00Z' -> None
```

- flag 以 key、规则以 `(flag, rule id)` 为身份；参数变化覆盖 `default`、
  `env` 覆盖值、规则的 `value/percentage/rollout/时间窗口` 与定义顺序。
- 返回 `ConfigDiff(added, removed, changed)`，条目按
  `(flag, scope, param)` 排序；`.empty / .total / .report()` 可直接断言，
  相同输入结论逐字节确定（自测连续 20 次对比报告完全一致）。

## 时间阶梯灰度（自动按时间抬比例）

规则可用 `rollout` 声明 `(time, percentage)` 档序列，替代静态 `percentage`
（二者互斥）：

```python
{"id": "gray", "value": True, "rollout": [
    {"time": "2026-10-01T00:00:00Z", "percentage": 0},
    {"time": "2026-10-02T00:00:00Z", "percentage": 10},
    {"time": "2026-10-04T00:00:00Z", "percentage": 50},
    {"time": "2026-10-08T00:00:00Z", "percentage": 100},
]}
```

- 求值时刻落在哪个档，就用该档比例解析分桶阈值；首档时间之前规则不生效。
  `snapshot.rollout_plan(flag, rule_id)` 返回当前档位、各档时间/比例/阈值。
- **越界配置编译期即被拒绝**（`ValueError`）：比例不在 `[0,100]`、
  时间不严格递增、比例回退（连万分桶精度内的实质回退，如
  `0.001% -> 0.0009%`，也拒绝）；允许持平形成 plateau。
- **分桶稳定、放量只扩不缩**：bucket 仍由 `(salt, flag, user)` 决定，
  与时间、进程、版本无关；阈值单调抬高意味着上一档已放量的用户
  在下一档必然仍放量。自测中 20000 用户跨 0%/10%/50%/100% 四档，
  放量集合逐档包含、bucket 跨实例完全一致、单个用户至多翻转一次。
  实测放量：0.00% → 10.08% → 50.03% → 100.00%（偏差 < 0.1pp）。

## 性能（十万次求值，本机 Python 3.12）

| 场景 | 总耗时 | 单次 |
|---|---:|---:|
| 冷缓存（首次，含 sha1 分桶） | ~0.14 s | ~1.4 µs |
| 热缓存（分桶命中缓存） | ~0.13 s | ~1.3 µs |

缓存与预编译策略：

- **编译期预编译**：`compile_config` 把 ISO 时间窗口解析为 epoch 秒、百分比
  预乘为万分桶阈值、规则冻结为不可变结构；求值期零解析、零拷贝。
- **分桶缓存**：`(flag, user_id) → bucket` 缓存在编译配置上（有界 20 万条，
  超限整体清空重建），重复用户免去 sha1 计算；缓存只是纯函数的备忘录，
  不影响正确性，且随旧配置一起被旧快照持有，隔离语义不变。
- **短路**：`percentage=0/100` 不计算哈希；无灰度规则时不分桶。
