# 解析器重构：单体函数 → 四阶段流水线

> 本文档由 `scripts/update_readme.py` 依据本模板自动生成（`README.md` 为产物），
> 其中所有统计数字均由脚本从代码与语料实时复算，请勿手工改动数字。

解析对象是一个布尔过滤表达式 DSL（示例：`age >= 18 and (name = 'alice' or not active = true)`）。
`legacy_parser.py` 是重构前的基线：一个函数内同时完成切词、结构判定、结果组装与报错。
`filter_parser/` 是重构后的实现，仅依赖 Python 3 标准库。

## 阶段划分与输入输出类型

| 阶段 | 模块 | 输入 → 输出 | 失败方式 |
|---|---|---|---|
| 1 切词 | `filter_parser/lexer.py` | `tokenize(text: str) -> list[Token]`（`Token(kind, value, pos)`，见 `tokens.py`） | 抛 `ParseError(category="lex")` |
| 2 结构判定 | `filter_parser/structure.py` | `parse_structure(tokens) -> Node`（不可变语法树，节点类型见 `nodes.py`） | 抛 `ParseError(category="syntax")` |
| 3 结果组装 | `filter_parser/assemble.py` | `assemble(node) -> dict`（对外 AST 字典） | 不失败（输入树必然合法） |
| 4 错误生成 | `filter_parser/errors.py` | `to_public(error, text) -> dict`（含 `category/message/pos/line/col`） | — |

`filter_parser/api.py` 的 `parse(text)` 把四个阶段串起来，对外契约与基线完全一致：
`{"ok": True, "ast": ...}` 或 `{"ok": False, "error": {...}}`。

**无共享可变状态**：各阶段都是纯函数，只通过参数/返回值通信；Token 用 `NamedTuple`、
语法树节点用 `frozen dataclass`，中间产物不可变；`structure` 内部的游标对象是每次调用的
局部对象，调用之间不保留任何状态（有对应单测 `test_no_shared_state_between_calls`）。

## 运行命令

```bash
# 分阶段单测（{{STAGES_COUNT}} 例：切词 / 结构 / 组装 / 错误 / API）
python3 -m unittest tests.test_stages -v

# 差分测试：{{CORPUS_TOTAL}} 条手工用例（正常 {{CORPUS_NORMAL}} / 边界 {{CORPUS_BOUNDARY}} / 畸形 {{CORPUS_MALFORMED}}）+ {{FUZZ_TOTAL}} 条模糊用例，逐例比对重构前后
python3 -m unittest tests.test_differential -v

# 全部测试
python3 -m unittest discover -s tests -v

# 耗时对比
python3 bench.py

# 重新生成本文档（所有数字自动复算）
python3 scripts/update_readme.py
```

## 差分测试设计

`tests/test_differential.py` 对同一输入分别调用 `legacy_parser.parse` 与
`filter_parser.parse`，用**类型严格**的深度比较（`strict_equal`，区分 `1` 与 `1.0`）
比对完整结果，包括错误分类、错误消息与 `pos/line/col` 三个位置字段。用例三类：

- 正常：优先级、结合性、括号、not、字符串转义、多行输入等（`tests/corpus.py::NORMAL`，{{CORPUS_NORMAL}} 条）
- 边界：空串、纯空白、无空格、深嵌套（30 层括号）、长链（50 个 and/or）、大整数、
  Unicode 字符串等（`BOUNDARY`，{{CORPUS_BOUNDARY}} 条）
- 畸形：缺操作数/操作符/值、括号不配对、未闭合字符串、非法转义、非法数字、
  非法字符、句尾多余 token 等（`MALFORMED`，{{CORPUS_MALFORMED}} 条），外加 {{FUZZ_GEN_COUNT}} 种模糊生成器
  （合法表达式 / 随机 token 汤 / 合法表达式单点变异，各种子 {{FUZZ_PER_GEN}} 条）

## 耗时对比（本机 Python {{PY_VERSION}}，{{WORKLOAD_SIZE}} 条混合输入 × {{BENCH_REPEAT}} 轮取最优）

| 实现 | 总耗时 | 单条均耗 | 比值 |
|---|---|---|---|
| legacy（单体） | ≈ {{LEGACY_MS}} ms | ≈ {{LEGACY_US}} µs | 1.00 |
| staged（重构后） | ≈ {{STAGED_MS}} ms | ≈ {{STAGED_US}} µs | ≈ {{RATIO}} |

重构后约慢 {{SLOWDOWN_PCT}}%（绝对值约 {{DIFF_US}} µs/条），代价来自多出的中间表示（Token 与语法树节点对象），
这正是换取可测性的部分；已通过 `Token` 改用 `NamedTuple`、节点使用 `slots` 等手段
压低开销，当前比值 {{RATIO}}，不属于显著变慢。运行 `python3 bench.py` 可复现。

## 扩展时的改动点说明

**新增一种语法特性**（例：增加 `contains` 运算符或 `in (1, 2, 3)` 列表值）：

- 改 `lexer.py`：识别新 token（新关键字/新运算符），`tokens.py` 加一个 kind 常量；
- 改 `structure.py`：在对应文法层级消费新 token，`nodes.py` 加一种节点；
- 改 `assemble.py`：为新节点加一条 `isinstance` 分支；
- 不改 `errors.py`：错误类型与对外格式不变。

不波及无关阶段的原因：阶段间只靠类型化接口（`list[Token]`、`Node`）通信。
新 token 只是 `kind` 的一个新取值，切词其余规则不动；新节点只是 `Node` 联合类型的
一个新成员，既有文法规则（and/or/not/括号）的代码路径不经过它；组装是纯函数分派，
新增分支不改变既有分支行为。

**新增一种错误类型**（例：标识符超长报 `name too long`）：

- 若属于既有分类（lex/syntax）：只在出错阶段的模块里加一个
  `raise ParseError(LEX, "name too long", pos)`，其余阶段零改动；
- 若属于新分类（如 semantic）：在 `errors.py` 的 `CATEGORIES` 中登记新分类常量，
  再在对应阶段抛出即可。`to_public` 对分类是泛化的（只做格式化与行列换算），
  无需修改；切词、组装等无关阶段完全不感知。

不波及的原因：错误在阶段内部以统一的 `ParseError(category, message, pos)` 表示，
对外格式的生成集中在 `errors.to_public` 一处；新增错误只是多一个抛出点，
不改变任何既有抛出点，也不改变格式化逻辑。

## 目录

```
legacy_parser.py        重构前基线（单体函数）
filter_parser/          重构后：tokens/lexer/structure/nodes/assemble/errors/api
tests/test_stages.py    分阶段单测（含异常输入）
tests/test_differential.py  差分测试（手工语料 + 模糊）
tests/corpus.py         正常/边界/畸形语料与模糊生成器
bench.py                耗时对比
README.template.md      本文档模板（数字为占位符）
scripts/update_readme.py  统计复算并生成 README.md
```
