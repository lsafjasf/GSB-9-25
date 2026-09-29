# displaywrap — 按显示宽度折行与截断（Python 3，仅标准库）

终端/表格排版场景下，中文、组合字符、零宽字符与表情符号混排时，按字符数
截断会错位。本库按**显示宽度**折行与截断，保证不可拆散单元（组合序列、
代理对、调用方声明的禁拆片段）整体移动，并支持与一次性处理结果完全一致
的流式折行。

## 运行命令

```bash
python3 -m unittest test_displaywrap -v   # 自测（66 项：字符类/折行/截断/断行规则/流式一致性）
python3 bench.py                          # 性能与内存基准
python3 demo_rules.py                     # 断行规则对照演示（混排文本 × 4 种规则）
python3 -c "from displaywrap import wrap; print(wrap('hello 世界 foo', 8))"
```

## API

| 函数 | 说明 |
| --- | --- |
| `display_width(text)` | 显示宽度（制表符计 0，需先 `expand_tabs`） |
| `clusters(text)` / `cluster_width(cl)` | 不可拆散簇切分与簇宽 |
| `expand_tabs(text, tabsize=8)` | 按显示列位展开制表符（每逻辑行重置） |
| `wrap(text, width, *, tab="expand", tabsize=8, atoms=(), rules="default")` | 一次性折行 → 行列表 |
| `truncate(text, width, *, ellipsis="…", tab="expand", tabsize=8)` | 截断并追加省略标记 |
| `Wrapper(width, ..., rules="default")` | 流式折行器：`feed(chunk) -> list[str]`，`finish() -> list[str]` |
| `BreakRule` / `register_rule(name, rule)` / `list_rules()` | 断行规则插件：基类 / 注册 / 列出 |

## 规则

- **宽度**：东亚宽/全角计 2 列；组合字符（Mn/Me）、零宽字符（Cf：
  ZWJ/ZWSP/ZWNJ/BOM）、控制字符计 0 列；其余计 1 列。
- **不可拆散单元**：基字符+附加标记、ZWJ 序列、旗帜（双区域指示符）、
  肤色/键帽序列整体为一簇；Python 3 中代理对是单码位，天然不拆；
  `atoms=[...]` 声明的禁拆片段整体移动，自身超宽时独占一行（允许溢出）。
- **折行**：优先在空格处断行；超宽单词按簇强制断开；行首/行尾及断点处
  空白丢弃；`\n` 硬换行（产生空行），`\r` 忽略。
- **截断降级规则**：`width < display_width(ellipsis)` 时返回空串 `""`；
  否则省略标记计入宽度，按簇截取，绝不拆簇；按单行处理（首个 `\n` 后忽略）。
- **制表符**（显式可选）：`tab="expand"` 按显示列位展开（与折行共用同一
  宽度计算）；`tab="reject"` 抛 `ValueError`。
- **断行规则**（`rules` 参数，按语言选择）：规则只影响断点选择，不改变
  显示宽度计算。内置 `"default"`（历史行为）、`"cjk"`（避头尾）、
  `"western"`（连字符断词）；可组合（如 `rules=("cjk", "western")`），
  也可继承 `BreakRule` 后用 `register_rule` 注册自定义规则。

## 断行规则插件

同一段混排文本在不同规则下的断行对照（`python3 demo_rules.py`，行宽 9）：

```
[default]      [cjk]          [western]      [cjk+western]
-------------  -------------  -------------  -------------
他说：“排      他说：“排      他说：“排      他说：“排
版（types      版（types      版（type-      版（type-
etting）       etting）       setting）      setting）
要避头尾       要避头尾，     要避头尾       要避头尾，
，否则pun      否则punct      ，否则pu-      否则punc-
ctuation       uation、b      nctuation      tuation、
、bracket      rackets会      、bracke-      brackets
s会错位。      错位。”        ts会错位       会错位。”
”
```

- **default**：只在空格/簇边界断行，标点、括号不特殊处理（`，`、`、` 可能
  出现在行首，`”` 可能孤立成行）。
- **cjk（避头尾）**：闭标点（`，。、）”` 等）不出现在行首——悬挂在上一行尾
  （允许该行轻微超宽）；开括号（`（《“` 等）不出现在行尾——整体挪到下一行。
- **western（连字符断词）**：超宽单词在 ASCII 字母间断开并在行尾插入 `-`
  （连字符占 1 列，不足时行尾字母为其腾位；连字符前至少保留两个字母）。
- **组合**：`rules=("cjk", "western")` 同时生效，适合中英混排。

注意：避头尾的悬挂策略意味着连续闭标点会延长当前行缓冲，内存上界相应
变为 O(width + 最长禁拆片段 + 最长簇 + 连续悬挂标点长度)。

## 字符类用例清单（test_displaywrap.py: TestCharClasses）

| 用例 | 示例 | 期望 |
| --- | --- | --- |
| ASCII / 拉丁 | `abc` | 宽 3 |
| CJK 宽字符 / 全角 | `中` `Ａ` `，` | 各宽 2 |
| 组合字符（Mn/Me） | `e`+U+0301、U+20E3 | 附加标记宽 0，簇 `é` 宽 1 不拆 |
| 零宽字符（Cf） | ZWSP/ZWJ/ZWNJ/BOM | 宽 0 |
| 变体选择符 | U+FE0F | 宽 0；`❤️` 一簇宽 2 |
| 代理对（星平面 emoji） | `😀` | 单簇宽 2，不拆 |
| ZWJ 序列 | `👨‍👩‍👧` | 单簇宽 2，不拆 |
| 旗帜（区域指示符对） | `🇨🇳` | 单簇宽 2 |
| 肤色修饰符 | `👍🏽` | 单簇宽 2 |
| 键帽序列 | `1️⃣` | 单簇宽 2 |
| 孤立代理项 | `\ud800` | 不崩溃，宽 1 独立簇 |
| 控制字符 | `\x00` `\t` | 宽 0（制表符由 tab 选项处理） |

## 流式一致性与内存上界

- 一致性由构造保证：`wrap()` 就是 `Wrapper.feed(全文)+finish()`；测试覆盖
  全部二/三分割点、逐字符喂入、300 轮随机模糊（含 atoms/制表符/emoji）。
- **内存上界**：内部缓冲为 `O(width + 最长禁拆片段 + 最长簇)`，与输入总量
  无关（输出行即产即弃时）。实测见下。

## 性能数据（Python 3.12，行宽 80，含 3 个禁拆片段）

| 输入 | 一次性折行 | 流式折行（4 KiB 块） | 库内部峰值内存 |
| --- | --- | --- | --- |
| 100,000 字符 | 98.9 ms（≈1.0 M字符/s） | 98.4 ms | — |
| 1,000,001 字符 | 940.9 ms（≈1.06 M字符/s） | 922.1 ms | 54.8 KiB |
| 4,000,000 字符 | — | — | 54.8 KiB（与 1M 相同） |

截断为亚毫秒级（1M 字符文本约 0.5 ms，找到首个换行即按单行处理）。
