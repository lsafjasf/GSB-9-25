# 字段增删改动点清单

## 唯一声明位置

`codec/fields.py` 的 `FIELDS` 元组是字段映射的**唯一**声明处：
tag、字段名、类型、默认值、payload 编解码函数全部集中在此。
`codec/core.py` 的 `encode` / `decode` 完全由它驱动，业务模块
（`modules/module_a.py`、`modules/module_b.py`）只调用 `codec.encode` /
`codec.decode`，不允许再写任何字段映射或序列化代码。

## 新增一个字段（例如 `nickname: str = ""`，tag=6）

只需修改一处：`codec/fields.py`，在 `FIELDS` 末尾追加一行

```python
Field(6, "nickname", TYPE_STR, "", _enc_str, _dec_str),
```

检查清单：
1. tag 取从未发布过的新值（历史 tag 永不复用，避免旧数据被误读）。
2. 选择已有类型（TYPE_INT / TYPE_STR / TYPE_FLOAT / TYPE_BOOL）；
   若是全新类型，需在同一文件新增 TYPE_* 常量与一对 `_enc/_dec` 函数
   （仅此情况需要同时动 `codec/fields.py` 的类型区，core 仍无需改）。
3. 给出安全的默认值，保证旧数据（缺失该字段）能正常解码。
4. 不要改 `codec/core.py`、`modules/*`、`legacy/*`。
5. 测试无需改动：`tests/gen.py` 遍历 `FIELDS` 自动覆盖新字段；
   可选：在 `tests/test_differential.py` 的历史字节夹具中追加固定用例。

## 删除字段

1. 不要直接删掉 tag 后复用；建议保留一行并注释为已废弃，或从 FIELDS 移除
   后在文档登记该 tag 永久占用。
2. 线上旧数据中残留的该字段会被解码端当作未知字段保留在 `_unknown`，
   不会报错、不会丢失。

## 兼容性约定（由 codec/core.py 保证）

- 多余（未知）字段：原样保留为 `record["_unknown"]` 中的
  `(tag, type_id, payload)` 三元组，重新编码逐字节写回。
- 缺失字段：按 `FIELDS` 中声明的 default 补齐，字段名记入
  `Record.defaults_applied` 并以 logging 输出（logger 名 `codec`）。
- 空值：任何字段值为 `None` 时编码为 TYPE_NULL(5)，解码还原为 `None`。
- 类型与声明不匹配：按未知字段处理（保留原始字节），已知字段走默认值。

## 本次重构改动点

| 文件 | 角色 |
| --- | --- |
| `codec/fields.py` | 新增：字段唯一声明处（改字段只动这里） |
| `codec/core.py` | 新增：统一 `encode` / `decode` / `Record` |
| `codec/__init__.py` | 新增：对外只暴露统一接口 |
| `modules/module_a.py` | 订单模块：删除内联实现，改为调用 `codec` |
| `modules/module_b.py` | 报表模块：删除内联实现，改为调用 `codec` |
| `legacy/module_a.py` | 重构前订单模块实现的冻结副本（仅差分基准，禁止修改） |
| `legacy/module_b.py` | 重构前报表模块实现的冻结副本（仅差分基准，禁止修改） |
| `tests/gen.py` | 新增：seeded 随机记录生成（空值/缺失/未知字段） |
| `tests/test_differential.py` | 新增：字节级差分 + 历史数据读入行为差分 |
| `tests/test_roundtrip.py` | 新增：500 例随机往返、逐字段比对、再编码稳定性 |
| `tests/test_compat.py` | 新增：未知字段保留、缺失默认值并记录、空值、坏魔数 |
