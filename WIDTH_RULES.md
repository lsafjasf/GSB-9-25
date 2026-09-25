# 显示宽度规则（src/table_align.py）

## 唯一宽度来源

`display_width(text)` 是唯一的宽度入口，内部先 `expand_tabs` 再逐字符经
`_char_width` 求和。`render`（对齐）、`truncate`（截断）、`wrap`（折行）、
`column_widths` 全部复用它，不存在第二份宽度实现（缺陷版曾在 4 处各写一份
`len()`，已全部消除）。

## 单字符宽度

| 字符类别 | 宽度 | 例子 |
|---|---|---|
| 东亚宽字符（`east_asian_width` ∈ {W, F}） | 2 | 中文、🙂 |
| 组合记号 / 零宽字符（Mn、Me、Cf、`combining()>0`） | 0 | U+0301、U+200B、ZWJ、变体选择符 |
| 其余控制字符（Cc、Cs） | 0 | — |
| 其他可打印字符 | 1 | a-z、0-9 |

## 制表符策略：按列位展开（全流程一致）

`\t` 一律在入口处由 `expand_tabs` 展开为空格，展开到 8 的倍数列位
（按显示列计，不是字符数）。对齐、截断、折行看到的是展开后的文本，
因此策略天然一致；`_char_width('\t')` 直接抛错，防止未展开的制表符泄漏。

## 不可拆散序列（集群）

`clusters()` 把文本切成简化版 grapheme 集群：base 字符 + 后续组合记号 /
变体选择符 / 肤色修饰符，ZWJ（U+200D）连接的 emoji 序列，以及成对的
区域指示符（旗帜）。`truncate` / `wrap` 只按集群推进：

- 宽度够 → 整个集群保留；
- 宽度不够 → 整个集群舍弃，绝不留下半个序列。

注意：ZWJ 序列的宽度定义为各分量宽度之和（如 👨‍👩‍👧 = 2+0+2+0+2 = 6）。
标准库没有 emoji presentation 数据，这是文档化的确定规则，保证全流程一致。

## 运行

```sh
python3 -m unittest discover -s tests -v   # 复现 + 回归测试
python3 compare.py                          # 生成修复前后对照（comparison_output.txt）
```
