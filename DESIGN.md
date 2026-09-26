# 设计说明与改动点分析

## 1. 巨石版本的具体痛点（对照 legacy/monolith.py）

1. 一个函数里顺序做词法扫描、递归下降（嵌套闭包）、语义校验、dict 组装，
   无法只喂 token 测语法、也无法只喂 CST 测语义。
2. 报错走内联闭包 `fail`，每次构造错误都在同一函数里重算行号列号；
   错误 code 字符串散落在几百行代码里，拼写/口径容易漂移。
3. 成功/失败控制流混在一起（解析闭包用 `isinstance(x, dict)` 传递错误），
   改表达式逻辑容易碰坏 LIMIT/SELECT 的处理。
4. 游标 `pos = [0]` 等可变状态与结果组装共享作用域。

## 2. 拆分后的阶段边界

```
source ──tokenize──▶ tuple[Token] ──parse──▶ Program ──assemble──▶ {"ok": True,...}
                          │                     │            │
                          └────── ParseError ◀──┴────────────┘
                                           │
                                      render(error, source)
                                           ▼
                                  {"ok": False, "error": {...}}
```

关键约束：

- **单向数据流**：后一阶段只接收前一阶段的不可变产物，无法回改 token/CST。
- **错误即值**：阶段只抛 `ParseError(code, offset, message)`，不感知
  行号列号和 JSON 形状；`render` 是唯一的“错误生成”出口。
- **门面极薄**：`api.parse_query` 只有一个 try/except，业务逻辑为零。

## 3. 各阶段可独立测试的证据

- 阶段 1：`tokenize("= <> != < <= > >=")` 直接断言 token 序列/偏移。
- 阶段 2：`parse(tokenize(text), len(text))` 后断言 `isinstance(w, cst.Or)`
  等 CST 形状；也可手工构造 `tuple[Token]` 喂给解析器，完全不需要源文本。
- 阶段 3：`tests/test_assembler.py::test_assembler_works_on_hand_built_cst_without_tokens`
  直接 new `cst.Program` 调用 `assemble`，证明组装不依赖词法/语法阶段。
- 阶段 4：`render(bad_escape(1,"q"), source)` 纯函数断言 offset→行列换算
  与负载字段。

## 4. 新增一种语法特性：以 `ORDER BY 列 [ASC|DESC]` 为例

| 文件 | 改动 | 为什么必须改 |
|---|---|---|
| `ql/lexer.py` | 加入关键字 `ORDER/BY/ASC/DESC` | 新关键字需要被识别为 KEYWORD |
| `ql/cst.py` | 增加 `OrderBy(columns: tuple[OrderItem,...])`，挂到 `Program` | 新结构需要在契约里有位置 |
| `ql/parser.py` | 子句循环增加 ORDER BY 产生式，`Program` 构造时传入 | 结构判定是唯一理解新产生式的地方 |
| `ql/assembler.py` | 输出 `"order_by"` 字段；如需“列必须出现在 SELECT”这类**语义**规则，在此校验 | 组装是唯一塑形输出/语义规则的地方 |

**不受影响的阶段**：

- `errorgen.py` 不动——非法的 ORDER BY 直接复用
  `E_UNEXPECTED_TOKEN`，位置仍由 `render` 统一换算。
- 切词的字符串/数字/标识符逻辑、WHERE 表达式逻辑不动。
- 旧调用方的错误负载形状不变（只有成功负载多一个键，属于特性本身）。

隔离成立的原因：语法知识只存在于 `parser.py`，输出形状只存在于
`assembler.py`，二者通过 `cst.py` 这个显式契约耦合；新增节点是纯增量
（给 `Program` 带默认 `None` 的新字段即可让旧构造点不破坏）。

## 5. 新增一种错误类型：以“列名不能是保留字（如 SELECT）”为例

| 文件 | 改动 |
|---|---|
| `ql/errors.py` | 增加 `ErrorCode.RESERVED_COLUMN = "E_RESERVED_COLUMN"` 与工厂 `reserved_column(offset, name)`（文案唯一出处） |
| `ql/parser.py` 或 `ql/assembler.py` | 在能最准确定位的阶段 `raise reserved_column(tok.offset, word)`；本例在结构阶段读列名时判定 |
| （可选）某阶段测试 | 增加该 code 的用例 |

**不受影响**：

- `errorgen.py` 不动——它对 code 是通用的，自动输出新 code、文案和行列；
  负载形状零变化，调用方按 `code` 分支即可增量处理。
- 其它错误工厂与其它三个阶段不动；不会出现巨石时代“改一处报错碰到词法
  扫描”的情况，因为错误目录是集中的纯数据。

若新错误属于**词法**（如新进制数字格式）就在 lexer 抛；属于**结构**在
parser 抛；属于**语义**在 assembler 抛——阶段归属规则固定，天然防止波及。

## 6. 等价性是怎么保证的

- 差分比较整个负载而非仅成功结果，错误的 code/message/offset/line/column
  全部逐字段比对。
- 重构过程中差分测试实际抓到过两个真实回归（均已按旧行为修正）：
  1. AND/OR 节点的 offset 在右操作数解析之后才取游标，取到了操作数而非
     关键字；
  2. 重复 LIMIT/WHERE 一度被静默接受/在错误位置报出。
- 模糊器固定随机种子并提交在仓库中，CI/本地结果可复现。

## 7. 测试矩阵（正常 / 边界 / 畸形）

- 正常：完整查询、优先级与结合性、括号、字符串转义、多列、负 LIMIT。
- 边界：空串/纯空白、注释到行尾、`LIMIT 0/1000/-0`、单列、嵌套括号、
  EOF 落在每个子句、关键字大小写。
- 畸形：非法字符、未闭合字符串/括号、非法转义、连写比较、悬空 AND、
  列重复、WHERE 常量、LIMIT 越界、子句重复/乱序、截断与字节插入变异。
