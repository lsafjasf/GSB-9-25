# 分隔符文本解析器 — 修复说明

## 文件

- `naive_parser.py` — 修复前的缺陷实现，仅用于复现问题（勿用于生产）。
- `dsv_parser.py` — 修复后的解析器（Python 3，仅标准库）。
- `test_repro.py` — 四类现网缺陷的复现用例（同一输入：naive 错、fixed 对）。
- `test_dsv_parser.py` — 回归测试（推断、解析、流式一致性、错误处理）。

## 四类缺陷与修复

| 缺陷 | 根因 | 修复 |
|---|---|---|
| 字段内分隔符被切开 | 按字符 raw split，不感知引号 | 引号感知状态机（`_Core`），引号内分隔符视为数据 |
| 引号内换行被当成记录结束 | 按物理行切分 | 状态机在 `QUOTED` 状态下把换行视为字段数据 |
| 转义引号处理错误 | 不还原双写引号 | `QUOTE_AFTER` 状态下遇引号判为转义，输出字面引号 |
| 单行与多行推断不一致 | 单行走 raw-count 兜底路径 | 单一推断算法，与行数无关；证据不足显式报错 |

## 推断依据（可解释）

`infer(text)` 对 5 个候选分隔符（`,` `\t` `;` `|` `:`）× 2 个候选引号（`"` `'`）
共 10 种组合逐一做严格试解析，按以下优先级排序：

1. **记录数**多者优先；
2. **游离引号**少者优先（字段内容里残留引号字符 = 该组合大概率判错了引号）；
3. **字段数**多者优先。

仅当某组合产生「所有记录字段数一致且 ≥ 2 列」的结构时才为有效候选：

- 无任何有效组合 → 抛 `InferenceError`（证据不足，不猜测）；
- 多个分隔符并列 → 抛 `InferenceError`（歧义，列出候选，要求显式指定）；
- 仅引号候选并列：数据中出现的引号字符胜出；都未出现则取默认 `"` 并在
  rationale 中注明「不影响输出」。

返回的 `FormatSpec.rationale` 为人类可读判定依据，`FormatSpec.stats` 保留全部
10 个候选的统计证据（记录数/字段数/一致性/游离引号数）供审计。示例：

```
delimiter=',', quotechar='"': 2 record(s) x 3 field(s), fully consistent;
1 of 10 candidate (delimiter, quote) combinations were structurally valid
```

## 流式一致性

`StreamParser.feed(chunk)` 为逐字符状态机，跨块状态（引号内、转义、`\r\n` 边界、
未闭合记录）全部保留在实例中，与分块大小无关。回归测试对同一内容按
1..16、64、1024、全长等分块喂入，断言与一次性 `parse()` 逐字段相同；
另对短文本穷举了全部分块大小。

## 非法输入处理

- 严格模式（默认）：引号未闭合、字段数不一致、闭合引号后出现多余字符 →
  抛 `ParseError`，携带 1 起始的记录号（`row`）与字段号（`col`）。
- 宽松模式 `on_error="skip"`：跳过非法记录，`ParseResult.skipped` 计数、
  `ParseResult.errors` 收集每条错误（含行号），绝不静默丢行。
- 已知文件有脏行时，推断要求结构一致，建议显式传入 `FormatSpec` 配合宽松模式。

## 运行命令

```bash
cd /home/administrator/gsb/uid5/B
python3 -m unittest discover -v          # 全部 30 个测试
python3 -m unittest test_repro.py -v     # 仅四类缺陷复现
python3 -m unittest test_dsv_parser.py -v  # 仅回归测试
```
