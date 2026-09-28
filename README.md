# GSB-9-25 — 小型规则引擎

纯 Python 3 标准库实现，无第三方依赖。

## 文件

- `rule_engine.py` — 规则引擎库（条件、动作、规则、引擎、执行轨迹）
- `test_rule_engine.py` — 自测（unittest，19 个用例）
- `demo.py` — 演示：构建规则集并打印完整执行轨迹
- `perf.py` — 性能测试（1000 / 2000 / 5000 条规则）

## 运行命令

```bash
python3 -m unittest test_rule_engine -v   # 运行自测
python3 demo.py                            # 查看执行轨迹样例
python3 perf.py                            # 查看性能数据
```

## 用法

```python
from rule_engine import And, Engine, Field, Flag, Or, Rule, Set

engine = Engine(conflict_strategy="priority")   # 或 "reject"
engine.add_rule(Rule(
    "vip-discount",
    And(Field("age").ge(18), Field("tier").in_(["gold", "platinum"])),
    [Set("discount", 0.30), Flag("vip")],
    priority=10,
))
result = engine.run({"age": 40, "tier": "gold"})
print(result.trace_text())      # 完整执行轨迹
print(result.state.values)      # 最终字段
print(result.state.flags)       # 最终标记
```

- 条件：`Field("x").eq/ne/lt/le/gt/ge(...)`、`in_(...)` / `not_in(...)`，
  以及 `And` / `Or` / `Not`（或运算符 `&` / `|` / `~`）逻辑组合。
- 动作：`Set(field, value)` 赋值、`Flag(name)` 打标记。
- 规则可通过 `add_rule` / `remove_rule` / `clear` 动态增删，立即生效。

## 执行语义

- 规则按 **优先级降序、声明顺序升序** 依次执行，顺序完全确定。
- 条件针对当前工作状态求值（前面规则的动作可影响后面规则的条件）。
- 条件未命中或求值出错（字段缺失、类型不可比较）时，规则被跳过并记录原因，
  **不产生任何副作用**，且不影响其他规则执行。
- 同一输入重复执行，轨迹逐字节一致（有测试验证）。

## 冲突消解

**冲突定义**：两条不同规则命中后，对同一字段赋予不同的值。
（同一规则内顺序覆盖自己的赋值不算冲突；不同规则赋相同值也不算。）

引擎创建时必须显式选择策略：

- `priority`（默认）：先执行的规则（高优先级 / 先声明）的赋值生效，
  后续冲突赋值被拒绝，并写入轨迹的 `[conflict]` 记录。
- `reject`：遇到冲突立即抛出 `ConflictError`，异常中携带冲突详情
  （字段、双方规则名、双方取值），冲突动作不会应用。

每条冲突记录包含：字段名、已生效值及其来源规则、试图写入的值及其来源规则、
消解方式，保证可追溯。

## 执行轨迹样例（`python3 demo.py`）

```
[matched] vip-discount
    change: set discount: '<missing>' -> 0.3
    change: flag vip: added
[matched] loyalty-discount
    change: rejected set discount (conflict, kept 0.3)
    change: flag loyal: added
[skipped] student-or-senior
    reason: condition not satisfied
[skipped] needs-region
    reason: condition error: field 'region' is missing
[conflict] field 'discount': rule 'loyalty-discount' tried 0.15, but rule
'vip-discount' already set 0.3 -> kept earlier assignment (higher priority / earlier declaration)
```

## 测试覆盖

`test_rule_engine.py` 共 19 个用例，覆盖：

- 规则集为空、全部规则命中、逻辑组合与集合成员；
- 条件求值出错（类型不可比较）、字段缺失 —— 规则跳过且**断言状态未被触碰**；
- 动作相互覆盖：高优先级优先、声明顺序破平、`reject` 抛错、同值不算冲突、同名规则冲突、
  同规则内顺序覆盖；
- 规则动态增删立即生效；
- 轨迹确定性（同一输入执行 6 次轨迹完全一致）；
- 副作用断言：未命中/求值出错的规则对 values 和 flags 零改动，输入 facts 不被修改。

## 性能数据（`python3 perf.py`，CPython 3.12，30 次运行）

| 规则数 | 中位耗时/次 | 平均耗时/次 |
|-------:|------------:|------------:|
| 1000   | ~1.4 ms     | ~1.7 ms     |
| 2000   | ~2.8 ms     | ~4.3 ms     |
| 5000   | ~22 ms      | ~34 ms      |

每条规则含一个 `And(比较, Or(成员, 比较))` 条件和两个动作，约半数规则命中。
耗时与规则数近似线性，千条规则量级单次执行在毫秒级。
