# ABAC：可解释的基于属性的访问控制

纯 Python 3 标准库实现，无第三方依赖。判定结果不再是黑盒的允许/拒绝，
而是附带完整解释：命中/未命中规则清单、决定结论的规则、被遮蔽的规则、
以及缺省策略是否生效。

## 规则模型

每条规则（`examples/rules.json`）：

```json
{
  "id": "deny-low-clearance",
  "effect": "deny",
  "priority": 90,
  "when": {
    "action": {"eq": "read"},
    "subject": {"clearance": {"lt": 3}},
    "resource": {"classification": {"eq": "confidential"}},
    "env": {"hour": {"between": [9, 17]}}
  }
}
```

- 四类条件：`subject.*`、`resource.*`、`action`、`env.*`，组内为合取。
- 操作符：`eq ne in not_in gt gte lt lte between contains prefix suffix matches exists`。
- 简写：`"action": "read"` 等价于 `"action": {"eq": "read"}`。
- 跨属性引用：`"owner": {"eq": "$subject.id"}` 比较资源属性和主体属性。
- `priority` 为显式整数优先级，越大越优先。

## 判定语义（确定性）

1. 候选规则中只要有规则因**属性缺失或类型不符**无法求值，立即按
   `config.on_attribute_error` 处理：`"error"`（默认，返回可区分的
   `error` 结论）或 `"deny"`。绝不默认放行，解释中通过
   `default_policy_applied` 与 `errored` 清单体现。
2. 否则取命中的最高优先级规则决定结论；同优先级冲突采用固定策略
   **deny-overrides**（拒绝优先）。理由：授权场景下宁可误拒不可误放，
   误拒可被解释清单追踪并修正，误放是安全事故。解释中
   `tie_break: "deny-overrides"` 标明该策略被触发。
3. 无任何规则命中时按 `config.default_effect`（默认 `deny`）处理，
   同样在 `default_policy_applied` 中体现。

## 矛盾检测（加载时报错）

- **同条件 + 同优先级 + 相反结论** → 真正的矛盾，加载时抛
  `RuleSetError`（见 `examples/conflict_rules.json`）。
- 同条件不同优先级可确定消解（高优先级胜），不算矛盾，不报错。
- 重复规则 id、未知操作符、非法参数（如 `between` 非二元数组、
  非法正则）也在加载时报错。

## 规则编译与复杂度

加载时把每条规则挂载到其"最具选择性的常量等值条件"上，构建哈希索引
`{属性路径: {常量值: [规则]}}`（路径优先级：action > resource.type >
resource.id > …）；无常量等值条件的规则进入兜底列表。单次判定时：

- 候选集 = 请求各索引路径值的哈希桶并集 + 兜底列表，
  代价 O(索引路径数)，与规则总数无关；
- 仅对候选规则逐条求值。规则均匀分布在索引键上时，单次判定接近
  O(规则总数 / 不同键数)，实测近似常数。

## 实测数据

`python3 -m abac bench --sizes 1000,10000,20000,50000 --requests 3000`
（Python 3.12，每次判定含完整解释生成；候选数固定约 25 条）：

| 规则数 | 候选/请求 | 索引 avg | 索引 p50 | 索引 p99 | 线性扫描 avg |
|-------:|----------:|---------:|---------:|---------:|-------------:|
|  1,000 |        25 |  22.9 µs |  22.9 µs |  44.3 µs |     0.99 ms |
| 10,000 |        25 |  42.6 µs |  39.8 µs |  87.4 µs |    10.06 ms |
| 20,000 |        25 |  33.7 µs |  33.1 µs |  61.6 µs |    19.75 ms |
| 50,000 |        25 |  45.1 µs |  41.7 µs |  98.7 µs |    51.71 ms |

规则数增长 50 倍，索引判定耗时基本持平；线性扫描同比放大 50 倍。

## 运行命令

```bash
# 单元测试（含索引 vs 线性扫描一致性的随机化对照测试）
python3 -m unittest discover -s tests -v

# 校验规则集（矛盾检测，冲突时退出码 2）
python3 -m abac validate --rules examples/rules.json
python3 -m abac validate --rules examples/conflict_rules.json

# 单次判定，输出完整解释（退出码：allow=0 deny=1 error=2）
python3 -m abac check --rules examples/rules.json --request examples/request_allow.json
python3 -m abac check --rules examples/rules.json --request examples/request_deny_tiebreak.json
python3 -m abac check --rules examples/rules.json --request examples/request_missing_attr.json --compact

# 性能基准
python3 -m abac bench --sizes 1000,10000,50000 --requests 3000
```

解释输出样例见 `examples/explanation_sample.json`。
