# 配置行解析规则说明

工具：`envparse.py`（Python 3，仅标准库）。解析形如 `键=值` 的配置行。
`buggy_envparse.py` 为旧版缺陷实现，仅用于复现测试。

## 行级规则

- 空行、纯空白行、首个非空白字符为 `#` 的行：注释/空行，跳过。
- 配置行以**第一个** `=` 分隔键与值；键两端的空白被忽略，键本身非空。
- 非法行抛出 `ParseError`，携带 1 起始的 `line`（行号）与 `col`（列号）：
  - 缺少 `=`：列指向行尾（期望出现 `=` 的位置）；
  - 键为空：列指向 `=` 所在位置；
  - 引号未闭合：列指向**起始引号**所在位置。

## 值规则

值取 `=` 之后的原始内容。除下述引号与转义语义外，值**不做任何改动**：
前后空格、内部分隔符（`,` `;` `:` 等）、值中其余的 `=` 全部原样保留。

- **单引号 `'...'`**：内部完全字面，不做任何转义（`\n` 就是两个字符），
  直到下一个 `'`。
- **双引号 `"..."`**：支持转义序列 `\\` `\"` `\n` `\t` `\r`；
  其余 `\x` 原样保留（含反斜杠）。
- 引号字符是语法，不进入结果；引号片段与字面片段直接拼接
  （`a"b c"d` → `ab cd`）。
- 引号可嵌套：一种引号内的另一种引号是普通字符
  （`"it's"` → `it's`，`'say "hi"'` → `say "hi"`）。
- 单引号、双引号之外，反斜杠是普通字符。

## 注释规则

`#` 在**引号外**、且位于值的开头或前一字符是空格/制表符时，为注释起始，
其后内容被丢弃（`#` 之前的空白仍属于值）。其余位置的 `#` 是普通字符。

- `A=1  # note` → `1  `（`#` 前空白保留）
- `A=a#b` → `a#b`（`#` 前无空白，字面）
- `A="a#b"` → `a#b`（引号内，字面）

## 空值与值缺失

- `KEY=` → `Entry(value="", has_value=True)`：显式空值。
- 缺少 `=` 的 `KEY` → `ParseError`（非法行，不再被静默当作空值）。
- 键不存在 → `Config.get_entry()` 返回 `None`，`Config.get(key, default)`
  返回 `default`；键存在但为空值时 `Config.get()` 返回 `""` 而非 `default`。
  调用方据此区分“空值”与“缺失”并选择默认值。

## 运行命令

```sh
# 全部测试（复现测试 + 回归测试）
python3 -m unittest test_envparse -v

# 只跑五类缺陷的复现测试（针对 buggy_envparse）
python3 -m unittest test_envparse.BugReproductionTests -v

# 只跑修复版的回归测试
python3 -m unittest test_envparse.QuoteRuleTests \
    test_envparse.CommentTests \
    test_envparse.ValueIntegrityTests \
    test_envparse.EmptyVsMissingTests \
    test_envparse.InvalidLineTests \
    test_envparse.MultiLineTests -v
```

## API 速览

```python
import envparse

entry = envparse.parse_line("A='hello world'")   # Entry(key='A', value='hello world', has_value=True)
cfg = envparse.parse_text("A=1\nEMPTY=\n")       # 或 envparse.parse_file("app.env")
cfg.get("A")              # '1'
cfg.get("EMPTY")          # ''      —— 空值
cfg.get("ABSENT", "dft")  # 'dft'   —— 缺失，走默认值
cfg.get_entry("ABSENT")   # None
```
