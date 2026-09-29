# logtpl — 日志模板抽取与聚类库

纯 Python 3 标准库实现，无第三方依赖。把日志行中的可变部分（时间、IP、
UUID、时长、路径、URL、版本号、十六进制、标识符、数字）替换为分类占位符，
按模板字符串聚类，并输出质量指标。

## 文件

- `logtpl.py` — 库源码（抽取 / 聚类 / 指标 / 报告）
- `test_logtpl.py` — 自测（14 个用例）
- `demo.py` + `sample_logs.txt` — 样例演示
- `sample_output.txt` / `sample_output_sort_kv.txt` — 模板输出样例
- `bench.py` — 百万行性能基准

## 运行命令

```bash
python3 -m unittest test_logtpl -v   # 自测
python3 demo.py                      # 默认模式聚类样例日志
python3 demo.py --sort-kv            # 结构化模式（合并字段换序的 k=v 日志）
python3 bench.py 1000000             # 百万行性能基准
```

## 设计要点

- **占位符分类**：`<DATETIME> <URL> <IP> <UUID> <DURATION> <PATH> <VER>
  <HEX> <ID> <NUM>`，结构化模式下另有 `<VAL>`（k=v 的值）。正则按固定
  优先级匹配，归类结果确定、与输入顺序无关。各模式还带"类别特异度"约束：
  更具体的形态不得抢占更宽泛类别也能解释的文本——`<VER>` 要求显式 `v/V`
  前缀或至少三段点分数字（`v1.2.3`、`2.10.0`），因此 `45.67`、`12.0`
  这类普通小数归 `<NUM>` 而非 `<VER>`（见
  `test_decimal_vs_version_in_one_line`）。
- **可还原**：`Template` 保存字面量/占位符片段序列，
  `render(extract_values(line)) == line` 逐字符成立（测试
  `test_roundtrip_*` 对全部样例行、超长行、嵌套引号行验证）。
- **顺序无关**：聚类键就是模板字符串本身，不依赖任何输入顺序状态；
  输出按 `(-count, template)` 排序。测试用 20 个随机种子打乱输入，
  断言模板集合、计数、占位符归类、未聚类清单完全一致。
- **质量指标**：覆盖率（被模板解释的行占比）、平均占位符数（按行加权）、
  未聚类行清单。判定规则：模板字面量部分字母数 < `min_literal_letters`
  （默认 1）视为"无稳定锚点"，进入 unclustered，不计入覆盖率。
- **字段换序**：默认**明确分开**——模板是位置敏感的，字面量骨架不同即
  不同模板（判定依据）；传 `sort_kv=True` 时先把相邻 `k=v` 词元按 key
  排序、值归为 `<VAL>`，换序日志归并为同一模板。两种策略都有测试。

## 样例输出（默认模式，节选）

```
total lines      : 20
templates        : 10
coverage         : 90.00% (18/20)
avg placeholders : 2.83
unclustered lines: 2

[     3] <DATETIME> ERROR db query failed after <DURATION> host=db-<NUM> retries=<NUM>
[     3] <DATETIME> INFO http <IP> "GET <PATH>" <NUM> <DURATION>
[     2] job <UUID> finished in <DURATION> exit=<NUM>
...
== unclustered lines ==
[     1] 12345
[     1] 404 500
```

完整输出见 `sample_output.txt`；`--sort-kv` 模式见
`sample_output_sort_kv.txt`（两条字段换序的登录日志合并为
`action=<VAL> ip=<VAL> status=<VAL> user=<VAL>`，模板数 10 → 9）。

## 性能数据（bench.py，1,000,000 行合成日志 / 40 种模板形状）

环境：Python 3.12.3，Linux x86_64 容器。

| 指标 | 数值 |
|---|---|
| 总耗时 | 67.9 s |
| 吞吐 | ≈ 14,700 行/秒 |
| 模板数 | 38 |
| 覆盖率 | 100.00% |
| 平均占位符数 | 3.00 |
| 峰值分配内存（tracemalloc） | 0.2 MB（流式处理，不缓存行） |
| 最大常驻内存 RSS | 34.2 MB |

聚类状态只与不同模板数成正比，与行数无关；内存占用对行数近似 O(1)。

## 边界覆盖

- 单行 / 全部不同 / 无变量行：`test_single_line`、`test_all_different_lines`、
  `test_no_variable_line_is_own_template`
- 超长行（20 万字符）往返还原：`test_very_long_line`
- 嵌套引号 + 结构化字段：`test_nested_quotes_and_structured`
- 顺序无关性：`test_order_independence`（20 个随机种子）
- 字段换序两种策略：`test_field_reorder_default_separate` /
  `test_field_reorder_sort_kv_merges`
