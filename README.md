# GSB-9-25 小型规则引擎

纯 Python 3 标准库实现，无第三方依赖。把业务判定从代码中抽离为「条件 + 动作」的规则声明，多规则命中时按确定性顺序执行，冲突按显式策略消解并全程留痕。

## 文件

- `rule_engine.py` — 引擎库（条件 DSL、规则执行、冲突消解、可解释执行轨迹、重放验证）
- `test_rule_engine.py` — 28 个自测（unittest）
- `demo.py` — 可解释轨迹样例 + 重放验证 + 性能基准

## 运行命令

```bash
python3 -m unittest test_rule_engine -v   # 运行自测
python3 demo.py                           # 查看轨迹样例与性能数据
```

## 规则模型

```python
from rule_engine import Rule, RuleEngine

engine = RuleEngine(conflict_strategy="priority")   # 或 "reject"
engine.add_rule(Rule(
    rule_id="vip_member",
    condition={"and": [
        {"field": "tier", "op": "in", "value": ["gold", "platinum"]},
        {"field": "active", "op": "==", "value": True},
    ]},
    actions=[{"set": "discount", "value": 0.2},     # 赋值
             {"flag": "vip"}],                       # 标记
    priority=10,
))
result = engine.run({"age": 20, "tier": "gold", "active": True})
result.state   # 输出状态（输入 facts 的副本，原 dict 不被修改）
result.flags   # 标记集合
result.trace   # 完整执行轨迹（可 JSON 序列化）
```

- 条件：字段比较（`== != < <= > >=`）、集合成员（`in` / `not_in`）、逻辑组合（`and` / `or` / `not`，可任意嵌套，短路求值）。
- 动作：`{"set": 字段, "value": 值}` 赋值；`{"flag": 名称}` 打标。
- 规则可动态增删：`add_rule` / `remove_rule(rule_id)`。

## 执行顺序与冲突消解

1. 按声明顺序逐条求值条件，记录命中/跳过及原因。
2. 命中的规则按 **优先级降序、声明顺序升序** 依次执行动作。
3. 两个**不同规则**对同一字段赋**不同值**即构成冲突，按构造引擎时指定的显式策略处理：
   - `priority`（默认）：先执行者（即高优先级；平级时先声明者）的值保留，后到的赋值被拒绝。冲突详情写入 `trace["conflicts"]`（字段、保留方/被拒方规则与值、策略），被拒动作在 `trace["actions"]` 中标记 `"applied": false, "reason": "conflict"`。
   - `reject`：发现冲突即抛出 `ConflictError`，异常携带 `conflicts` 列表与截至失败点的完整 `trace`，冲突赋值不落盘。
4. 同一规则内对同一字段多次赋值属于自身覆盖，后者生效，不计冲突；不同规则赋**相同**值是幂等，不计冲突。

## 可解释执行轨迹

`trace` 可 JSON 序列化，包含：

- `evaluations`：每条规则 matched/skipped 及原因（`condition_false` / `field_missing: <字段>` / `condition_error: <详情>`），并带 `condition` **逐项求值树**——每个比较节点记录字段、操作符、期望值、实际值与结果；`and`/`or`/`not` 节点记录组合结果与每个子项；被短路的子项以 `unevaluated` 占位节点保留（`reason: short_circuit`），求值错误落在节点 `error_type`/`error` 上。
- `matched`：按执行顺序的规则 id。
- `actions`：每次动作的字段、旧值、新值、是否生效。被冲突拒绝的动作带 `overridden_by: <胜出规则>`；被后续赋值覆盖的已生效动作带 `superseded_by: <最终写入规则>`。
- `conflicts`：消解明细（字段、保留方/被拒方规则与值、策略）+ `basis` 消解依据（优先级高低，或平级时声明顺序先后）。
- `field_sources`：最终状态中每个字段的来源——`{"source": "input"}`（来自输入）或 `{"source": <规则id>, "action_index": <动作序号>}`。
- `final_state` / `final_flags`。

轨迹不含时间戳等不确定因素，同一输入多次执行逐字节一致（有测试断言）。`format_trace(trace)` 可把轨迹渲染为人类可读的逐行解释（见 `demo.py` 输出）。

## 决策重放

`engine.replay(facts, trace)` 用相同输入与规则集重新求值，要求逐字节复现给定轨迹（结论与解释完全一致），否则抛出 `ReplayMismatchError` 并列出分歧的顶层键。`reject` 策略下记录的是 `ConflictError.trace`，重放同样复现该失败轨迹。

## 无副作用保证

条件未命中、字段缺失或求值出错的规则不执行任何动作。测试 `test_skipped_rules_leave_state_untouched` 断言：输出状态与输入完全一致、标记集为空、动作列表为空、输入 dict 未被修改。

## 覆盖的情形

空规则集、全部命中（验证执行顺序）、条件求值出错（未知操作符、类型不可比较）、字段缺失、动作相互覆盖（规则间冲突 + 规则内覆盖）、动态增删、轨迹确定性、2000 条规则性能。

## 性能数据

Python 3.12，本机实测（`python3 demo.py`，20 次取平均，含轨迹记录与冲突检测）：

| 规则数 | 命中 | 动作 | 冲突 | 单次耗时 |
|-------:|-----:|-----:|-----:|---------:|
| 1000 | 667 | 1334 | 603 | ~2.0 ms |
| 2000 | 1333 | 2666 | 1269 | ~4.5 ms |
| 5000 | 3333 | 6666 | 3269 | ~18 ms |

复杂度与规则数近似线性；上千条规则单次执行在毫秒级。
