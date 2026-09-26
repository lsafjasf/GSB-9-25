# 结构化数据序列化：格式说明与兼容性边界

## 文件清单

| 文件 | 说明 |
|---|---|
| `serializer_buggy.py` | 修复前的旧实现（v1），保留作回归对照，禁止生产使用 |
| `serializer.py` | 修复后的实现（格式 v2），含 v1 历史数据只读兼容 |
| `test_serializer.py` | 复现用例 + 回归测试（26 个用例） |

运行：`python3 -m unittest test_serializer -v`（或 `python3 test_serializer.py`），仅标准库。

## 修复前后对照

| 现网问题 | 修复前（v1） | 修复后（v2） |
|---|---|---|
| 多字节/转义字符串内容改变 | 反斜杠不转义、`\uXXXX` 解码端不还原 | UTF-8 长度前缀 `S<n>:`，不依赖转义，任意字节序列无损往返 |
| 大整数精度丢失 | 一律经 `float()`，超 2^53 即截断 | 十进制文本直写 `I<digits>;`，任意精度 |
| 共享引用变多份拷贝 | 无引用表，按值展开 | 引用表 `R<n>;`，`is` 关系往返保持 |
| 循环引用栈溢出 | 无限递归 → `RecursionError` | 引用表确定性支持；另有 `MAX_DEPTH=200` 兜底，任何输入不栈溢出 |
| 非法输入无法定位 | 统一 `parse failed` | `ParseError` 携带字节偏移 `offset` 与期望结构 `expected` |

## 格式 v2 语法

```
value   = "N"                       # None
        | "T" | "F"                 # bool
        | "I" "-"? digits ";"       # 任意精度整数
        | "D" float-repr ";"        # float（repr 往返精确）
        | "S" digits ":" <n 字节 UTF-8 原始内容>   # 长度前缀字符串
        | "L" digits ";" value*n    # list
        | "M" digits ";" (value value)*n           # dict（键必须为 str）
        | "R" digits ";"            # 引用表中第 n 个容器
```

引用表语义：list/dict 按**先序**（首次出现的顺序）从 0 编号；同一对象再次出现时
只写 `R<id>;`。解码端在解析容器内容**之前**先把空容器登记入表，因此自引用与
相互引用都能正确重建。编解码均为 O(n)。

## 循环引用策略：引用表支持（而非显式拒绝）

选择理由：

1. 本实现用于**缓存与日志**，共享子结构（同一份配置、同一条记录被多处引用）
   是常态；显式拒绝会把合法数据变成错误，引用表同时解决了「共享引用变拷贝」。
2. 引用表是**确定性**策略：同一对象图序列化结果唯一，往返后拓扑完全等价
   （测试用 `is` 断言）。
3. 不会栈溢出：循环由 memo 查表截断，天然无无限递归；另设 `MAX_DEPTH=200`
   深度上限，超限抛 `EncodeError`/`ParseError` 而非 `RecursionError`。

## 错误报告

`ParseError` 携带三个属性：`offset`（字节偏移）、`expected`（期望结构）、
`found`（实际内容），消息形如：

```
parse error at byte offset 3: expected 5 bytes of string payload, got '2 bytes remaining'
```

覆盖：未知标签、非法数字、截断的容器/字符串、悬空引用 `R<n>;`、非法 UTF-8、
尾部垃圾、嵌套过深。

## 兼容性边界

- **v1 历史数据 → v2 代码：可读。** `loads()` 按首字节自动识别：非 v2 标签的
  输入走 v1（JSON 子集）只读解析器，同样报带偏移的错误。v1 写出的 `\uXXXX`
  非 ASCII 字符可正确还原。
- **已损坏的 v1 数据：不可恢复。** 若历史数据在**写入时**已触发旧缺陷
  （字符串转义被吞、大整数被 float 化），损坏发生在写路径，任何解析器都
  无法还原原始值——只能重新生成。v1 数字一律按 float 读回（与写出时一致）。
- **v2 数据 → v1 旧代码：不可读。** 旧代码会报 `parse failed`。升级需灰度：
  先全量部署可读 v2 的新代码，再切换写路径；或过渡期双写。
- **类型边界：** 仅支持 None / bool / int / float / str / list / dict（str 键）。
  其他类型（含非 str 键）抛 `EncodeError`。`float("nan")` 可序列化但
  `nan != nan`，不参与等价断言；`inf`/`-inf` 往返精确。
- **深度边界：** 嵌套超过 200 层在编码端与解码端均被确定性拒绝。
