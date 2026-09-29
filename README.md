# 查询 DSL 解析器：单函数巨石 → 四阶段流水线（重构 + 回归）

一个约 350 行的函数 `parse_query`（切词、结构判定、结果组装、报错混杂、
行号列号在每个 `raise` 处重复计算）被重构为职责单一、类型明确、可独立
单测的四个阶段，并以重构前的巨石版本为基线做差分回归。

仅使用 Python 3 标准库（`dataclasses`、`typing`、`unittest`、`random`、
`time`、`json`）。

## 目录结构

```
ql/                      重构后的实现
  errors.py              阶段 0：错误目录（code + 文案工厂，不含位置渲染）
  lexer.py               阶段 1：切词   str -> tuple[Token, ...]
  cst.py                 阶段间数据契约：不可变 NamedTuple 节点
  parser.py              阶段 2：结构判定  tuple[Token] -> Program (CST)
  assembler.py           阶段 3：结果组装 + 语义校验  Program -> 结果 dict
  errorgen.py            阶段 4：错误生成  (ParseError, source) -> 错误 dict
  api.py                 门面：串起四个阶段（parse_query）
  __main__.py            CLI：python3 -m ql 'SELECT ...'
legacy/monolith.py       重构前基线（禁止“顺手优化”，仅作差分对照）
tests/
  test_lexer.py          阶段 1 单测（正常/边界/畸形）
  test_parser.py         阶段 2 单测（正常/边界/畸形）
  test_assembler.py      阶段 3 单测（正常/边界/畸形）
  test_errorgen.py       阶段 4 单测（分类、行号列号、文案稳定性）
  test_api.py            门面与“无共享可变状态”回归
  test_differential.py   差分测试：46 个手工用例 + 8000 条种子模糊用例
benchmark.py             重构前后耗时对比（含分阶段耗时与 15% 预算守卫）
DESIGN.md                阶段契约、改动点说明、扩展性分析
```

## 阶段契约（输入 → 输出，均不可变、无全局可变状态）

| 阶段 | 模块 | 输入类型 | 输出类型 | 失败时 |
|---|---|---|---|---|
| 切词 | `ql.lexer.tokenize` | `str` | `tuple[Token, ...]` | `raise ParseError`（词法 code） |
| 结构判定 | `ql.parser.parse` | `tuple[Token,...]`, `int`(EOF 偏移) | `cst.Program` | `raise ParseError`（结构 code） |
| 结果组装 | `ql.assembler.assemble` | `cst.Program` | `{"ok": True, "query": ...}` | `raise ParseError`（语义 code） |
| 错误生成 | `ql.errorgen.render` | `ParseError`, `str` | `{"ok": False, "error": {...}}` | 纯函数，不失败 |

- `Token` 与全部 CST 节点是 `NamedTuple`（值语义、不可变）；`ParseError`
  是 `frozen=True` dataclass。阶段之间只传值，没有共享游标/结果字典。
- 解析器的游标是 `Parser` 实例的局部状态，每次调用新建实例
  （`tests/test_api.py::test_no_shared_state_between_calls` 守卫）。
- 行号/列号只在 `errorgen.render` 一处由偏移换算，错误文案只在
  `ql.errors` 一处定义，杜绝“同一错误两种报法”。

## 语言与错误目录

语法：`SELECT 列[,列...] FROM 表 [WHERE 表达式] [LIMIT 整数]`，
表达式含标识符、数字、双引号字符串（`\" \\ \n \t` 转义）、括号、
比较运算 `= <> != < <= > >=`（不可连写）、`AND/OR`（OR 优先级最低）、
`-- 行注释`；关键字大小写不敏感，标识符保持原样。

| code | 阶段 | 含义 |
|---|---|---|
| `E_UNEXPECTED_CHAR` | 切词 | 非法字符（位置=该字符） |
| `E_UNCLOSED_STRING` / `E_BAD_ESCAPE` | 切词 | 字符串未闭合 / 非法转义 |
| `E_UNEXPECTED_TOKEN` | 结构判定 | 期望/实际不符（含子句重复） |
| `E_UNCLOSED_GROUP` | 结构判定 | 括号未闭合 |
| `E_DUPLICATE_COLUMN` | 组装 | SELECT 列重复（位置=第二处） |
| `E_WHERE_CONSTANT` | 组装 | WHERE 不引用任何列 |
| `E_LIMIT_RANGE` | 组装 | LIMIT 不在 0..1000 |

错误负载统一为：
```json
{"ok": false, "error": {"code", "message", "offset", "line", "column"}}
```

## 运行命令

```bash
# 全部测试（77 个：分阶段单测 + 门面 + 差分 + 恢复模式）
python3 -m unittest discover -s tests -v

# 只跑差分测试（46 手工 + 8000 模糊；随机种子固定，可复现）
python3 -m unittest tests.test_differential -v

# 耗时对比（默认每轮 48000 次解析，取 5 轮中位数）
python3 benchmark.py
python3 benchmark.py 2000     # 自定义轮数

# 手工试用
python3 -m ql 'SELECT a FROM t WHERE a = 1 LIMIT 5'
python3 -m ql --recover 'SELECT a, 1, b FROM t WHERE a = @ LIMIT 5'
```

## 错误恢复模式（strict / recover）

`parse_query(source, mode=...)` 支持两种模式，默认 `mode="strict"`，
行为与此前版本逐字节一致（差分测试守卫）。`mode="recover"` 时解析器
跳过无法识别的片段继续向后扫描，产出**部分结果 + 错误清单**：

```python
from ql import parse_query
out = parse_query("SELECT a, 1, b FROM t WHERE a = @ LIMIT 5", mode="recover")
# out == {
#   "ok": False,                       # 有错即为 False；没错时等价严格模式
#   "query": {...},                    # 成功解析的子句，形状与严格模式相同
#   "errors": [                        # 按源码位置排序
#     {"code", "message", "offset", "line", "column",
#      "expected", "actual"}, ...      # 期望/实际为结构化字段（词法错误为 null）
#   ],
# }
```

恢复策略：每个阶段（切词/结构/语义）各记录本阶段错误后继续——非法
字符跳过单字符、坏字符串跳到字面量结束、结构错误跳到同步点（子句
关键字 `FROM/WHERE/LIMIT`、逗号或 EOF）、语义错误丢弃非法字段
（重复列只留首个、常量 WHERE 与越界 LIMIT 置空）。

一致性保证（`tests/test_recovery.py` 固定种子可复跑）：

- **前缀一致**：输入合法时，恢复模式结果与严格模式完全一致且错误
  清单为空；给合法查询追加垃圾后缀，已解析字段不变。
- **错误不丢**：严格模式报出的错误（code/offset/message）必然出现在
  恢复模式的错误清单中（8000 条种子模糊用例验证）。
- **定位准确**：每条错误都带 `line`/`column`（1 起），与严格模式同一
  处换算逻辑（`ql/errorgen.py`）。

## 差分测试与耗时结果

差分策略：`legacy.monolith.parse_query` 与 `ql.parse_query` 对同一输入
比较**整个负载**（成功结构或 `code/message/offset/line/column` 全字段）。
除 46 个手工用例外，还有两路固定种子模糊（语法偏向生成 4000 条 +
字节级变异 4000 条，测试内）；开发期额外跑过 4 万 + 12 万条均零差异。

耗时（本机 `Python 3.12.3`，`benchmark.py 3000`，5 轮中位数，两次运行）：

| 实现 | us/call（运行1） | us/call（运行2） |
|---|---|---|
| 巨石基线 | 9.050 | 7.904 |
| 四阶段重构 | 8.480 | 8.618 |
| new/old | 0.94x | 1.09x |

拆分带来的额外开销是两次普通函数调用与几次不可变对象构造，实测在系统
噪声内（脚本以 1.15x 为硬预算守卫）。新版分阶段占比约为
切词 48% / 结构 35% / 组装 13% / 错误渲染 4%。

## 扩展性（详见 DESIGN.md）

- 新增一种语法（如 `ORDER BY`）：改 `cst.py`（加节点）、`parser.py`
  （加产生式）、`assembler.py`（输出该字段）；切词器与错误渲染**不动**，
  错误目录通常只需复用 `E_UNEXPECTED_TOKEN`。
- 新增一种错误类型（如“保留字不能当列名”）：在 `errors.py` 加一个 code
  与文案工厂，在产生错误的阶段 `raise` 即可；位置换算、负载形状、其他阶段
  全部不动。
