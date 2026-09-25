# ABAC 权限判定引擎（含判定解释）

纯 Python 3 标准库实现，无第三方依赖。包：`abac/`；CLI：`python3 -m abac`。

## 规则模型

每条规则声明主体 / 资源 / 动作 / 环境四类属性条件的组合，并带显式整数优先级：

```json
{
  "id": "r-team-read",
  "effect": "allow",
  "priority": 40,
  "conditions": {
    "subject":     {"dept": ["eng", "ops"], "clearance": {"op": "ge", "value": 2}},
    "resource":    {"type": "document", "classification": {"op": "le", "value": 2}},
    "action":      "read",
    "environment": {"hour": {"op": "between", "value": [9, 18]}}
  }
}
```

条件写法：标量（严格类型相等）、数组（成员匹配）、`{"op": ...}` 运算符
（`eq ne in lt le gt ge between exists`）。`subject`/`resource`/`environment`
为属性对象，`action` 直接写条件。

## 冲突消解（确定性）

单条请求的判定顺序完全确定，与规则在文件中的先后顺序无关：

1. **优先级高者胜**；
2. **同优先级拒绝优先（deny-overrides）**——理由：失败封闭（fail-closed）。
   平局时若允许优先，一次配置失误就会放大为越权放行；拒绝优先把歧义
   收敛到安全侧，排查时再通过解释调整优先级；
3. 同优先级同结论时按规则 id 字典序取先，保证结果可复现。

被更高优先级（或平局策略）压过的命中规则会列入解释的 `shadowed_rules`，
并注明被谁遮蔽、因为什么。

**矛盾检测**：条件集完全相同（数组顺序无关、键序无关）而结论相反的规则
属于逻辑矛盾，加载时直接抛 `RuleConflictError`，列出涉事规则 id，
见 `examples/rules_conflict.json`。

## 属性缺失 / 类型不符

缺属性或类型不符**永不默认放行**，由显式策略 `--on-missing-attribute` 控制：

- `error`（默认）：判定结果为可区分的 `error`（CLI 退出码 2）；
- `deny`：判定为拒绝；
- `skip`：该规则按未命中处理，原因记入解释。

所用策略与触发的属性、问题类型（missing/type）都写入解释的
`policies` 与 `attribute_problem` 字段。无任何规则命中时应用
`--default-effect`（默认 `deny`），同样在解释中体现。

## 规则编译与复杂度

加载时规则按 **(action, resource.type) 二级哈希索引**编译：无条件（或
条件非等值形式）的维度进 `*` 通配桶；每个桶在编译期按上述消解顺序
预排序。一次判定的开销：

- 请求两个索引维度齐全：4 次哈希取桶 + 合并去重 + 桶内求值，
  **O(桶内规则数)**，与规则总数无关（接近常数）；
- 索引维度缺失：该维度退化为全量扫描，并在解释中标记 `index_degraded`，
  保证缺失属性策略在规则求值阶段生效而不是被索引静默剪枝。

被索引剪掉的规则数记入解释的 `pruned_by_index`。

## 实测数据

环境：Python 3.12.3，Linux x86-64。合成规则集（每桶约 20 条规则，
50+ 个动作 × 数十种资源类型），每规模 2000 次请求，含预热：

```
$ python3 -m abac bench --sizes 1000,10000,100000 --requests 2000
rules      requests   avg(us)    p50(us)    p95(us)    max(us)
1000       2000       28.1       27.0       33.7       66.3
10000      2000       29.4       28.3       36.0       83.3
100000     2000       91.3       89.0       109.5      253.2
```

规则数放大 100 倍，单次判定均值仅从 28µs 到 91µs（亚线性，差额主要
来自内存/缓存效应），验证接近常数级。编译为一次性线性开销：
1k / 1 万 / 10 万条分别约 10ms / 108ms / 1447ms。

## 运行命令

```bash
python3 -m unittest discover -s tests          # 自测（19 个用例）
python3 -m abac validate --rules examples/rules.json          # 加载+矛盾检测
python3 -m abac check --rules examples/rules.json \
    --request examples/request.json --on-missing-attribute skip
python3 -m abac bench --sizes 1000,10000,100000 --requests 2000
```

`check` 退出码：allow=0，deny=1，error=2，规则集错误=3。

## 文件清单

- `abac/conditions.py` — 属性条件匹配（缺失/类型问题抛出，绝不静默放行）
- `abac/model.py` — 规则解析、校验、矛盾检测
- `abac/engine.py` — 索引编译、判定、解释生成
- `abac/cli.py` / `abac/bench.py` — 命令行与基准
- `examples/rules.json` — 规则集样例（含优先级遮蔽与同优先级平局）
- `examples/request.json` / `examples/explanation.sample.json` — 请求与解释样例
- `examples/rules_conflict.json` — 矛盾检测用例（同条件相反结论）
- `tests/test_abac.py` — 自测
