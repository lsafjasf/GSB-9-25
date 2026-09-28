# cliparse 使用与解析规则

纯标准库 Python 3 命令行参数解析库。无第三方依赖。

## 运行

```sh
python3 -m unittest test_cliparse -v     # 运行全部边界用例测试
python3 examples/error_samples.py        # 打印错误提示样例(已生成 ERROR_SAMPLES.md)
```

## 快速上手

```python
from cliparse import Parser, ParseError

p = Parser(prog="deploy", exit_on_error=False)
p.add_option("--verbose", "-v")                          # 布尔开关
p.add_option("--env", "-e", takes_value=True, default="staging")
p.add_option("--include", "-I", takes_value=True, repeat=True)  # 重复收集
p.add_option("--replicas", "-r", takes_value=True, type=int, default=1)
p.add_positional("service")                              # 必需位置参数

rb = p.add_subcommand("rollback")                        # 子命令
rb.add_option("--steps", "-s", takes_value=True, type=int, default=1)

try:
    r = p.parse(["-v", "web", "rollback", "-s", "2"])
except ParseError as e:
    print(e.format())          # usage + error
    sys.exit(e.exit_code)      # 退出码由调用方决定
```

## 支持的写法

| 写法 | 示例 |
|---|---|
| 长选项 + 空格值 | `--env prod` |
| 长选项 + 等号值 | `--env=prod`、`--env=`(空字符串) |
| 短选项 + 空格值 | `-e prod` |
| 短选项 + 紧贴值 | `-eprod`、`-e=prod` |
| 短选项合并 | `-vfq`(全为开关)、`-vfe prod`(最后一个带值) |
| 布尔开关 | `--verbose` / `-v`,缺省 `False` |
| 重复选项 | `repeat=True` 时按出现顺序收集为列表;否则后者覆盖前者 |
| 位置参数 | 与选项任意交错:`web --verbose -e prod` |

## 明确规定(均有测试覆盖)

1. **选项可出现在位置参数之后**:解析不按位置截断,选项与位置参数可任意交错。
2. **以减号开头的值**:
   - `--opt=-任意值` 永远合法(等号后内容原样作为值)。
   - 空格分隔时,下一个 token 若"看起来像选项"则报 `requires a value`,不会被吞掉。
   - 负数(`-1`、`-3.14`)与单独的 `-` 不算选项,可直接作值或位置参数。
3. **空字符串值**:`--env=`、`-e=`、`--env ""` 与 `-e ""` 都得到 `""`;
   其中等号后为空(`-e=`)表示「内联空字符串」,与 `-e` 后缺值的报错严格区分。
4. **双横线终止符**:`--` 之后的所有内容(包括 `--xxx`、`-x`)一律按位置参数处理。
5. **子命令与全局选项的优先级**:以子命令 token 为界——之前的参数由全局解析器处理,
   之后的全部交给子命令解析器。同名选项(如全局 `-v` 与子命令的 `-v`)互不影响:
   写在子命令名前生效的是全局定义,写在子命令名后生效的是子命令定义。
   全局值在 `result.<name>`,子命令值在 `result.sub.<name>`。
6. **重复指定同一选项**:非 `repeat` 选项后者覆盖前者;`repeat=True` 的带值选项收集为列表,
   `repeat=True` 的开关统计出现次数。

## 错误与退出行为

- 未知选项(附拼写建议)、缺少必需值、值类型非法、必需位置参数缺失、缺少子命令,
  均产生 `usage: ...` + `error: ...` 的提示,见 `ERROR_SAMPLES.md`。
- `exit_on_error=True`(默认):打印到 stderr 并 `SystemExit(2)`;`-h/--help` 打印帮助并
  `SystemExit(0)`。
- `exit_on_error=False`:抛出 `ParseError`(带 `.message` / `.usage` / `.exit_code=2`)
  或 `HelpRequested`(带 `.text` / `.exit_code=0`),由调用方决定如何退出,便于库内复用与测试。
