# msgfmt — 按语言配置渲染界面文案的消息格式化库

纯 Python 3 标准库实现，无第三方依赖。

## 文件

- `msgfmt.py` — 库源码（模板解析、复数分档、数字/日期本地化、回退链）
- `locales.json` — 语言配置样例（en / zh / ru / fr，三种不同复数分档方式）
- `messages.json` — 多语言消息样例（同一消息在各语言中占位符顺序不同）
- `test_msgfmt.py` — 自测（unittest，22 个用例）
- `bench.py` — 性能基准（渲染 100,000 次）

## 运行

```bash
python3 -m unittest test_msgfmt -v   # 运行自测
python3 bench.py                     # 运行性能基准
```

## 用法

```python
from msgfmt import Formatter

fmt = Formatter.from_files("locales.json", "messages.json")
r = fmt.format("ru", "files.selected", user="Иван", count=1.5)
print(r.text)                   # Иван выбрал(а) 1,5 файла
print(r.locale_used)            # 实际使用的语言（回退后可能与请求不同）
print(r.plurals[0].explanation) # n=1.5 matches rule #0 (n is not integer) -> other
```

## 设计要点

- **具名占位符**：`{user}`、`{count, plural, one{# file} other{# files}}`；
  缺失 / 多余参数抛 `MessageFormatError` 并报名称与模板位置，重复使用进 `result.warnings`。
- **复数规则声明式声明**：每条规则是条件列表（`eq` / `mod_eq` / `mod_in` / `mod_not_in` /
  `is_integer` / `not_integer` / `always`），按序匹配，结果带可解释说明。
  样例含三种分档：zh（仅 other）、en（one/other）、ru（one/few/many/other）。
- **数字与日期本地化**：千分位、小数位、日期顺序全部来自 `locales.json`，代码无语言硬编码；
  回退渲染时按**实际使用语言**的配置格式化。
- **回退链**：每个语言声明 `fallback` 列表，消息缺失时按序回退，
  `RenderResult.locale_used` 标注实际语言，`chain` 记录尝试路径。
- **非法配置**：加载时校验（缺 `other`、无兜底规则、未知操作符、日期顺序非法、
  回退目标不存在等），抛 `ConfigError`。

## 性能（Python 3.12，本机实测）

渲染 `inbox.summary`（含复数 + 数字 + 日期）100,000 次：

- 总耗时约 0.51 s，单次约 5.1 µs，吞吐约 196,000 次/秒（模板只编译一次并缓存）。

---

## pool_fix/ — 连接池归还复位修复（独立交付）

连接归还时残留事务/超时/缓冲等状态的缺陷复现与修复，Python 3 标准库。
详见 `pool_fix/README.md`；运行：`cd pool_fix && python3 -m unittest test_pool_reset -v`。
