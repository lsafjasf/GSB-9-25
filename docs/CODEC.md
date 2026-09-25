# 统一编解码层说明

## 结构

| 路径 | 角色 |
| --- | --- |
| `src/codec.py` | 统一编解码接口 `encode_record` / `decode_record`，**字段映射唯一声明点 `FIELDS`** |
| `src/user_module.py` / `src/order_module.py` | 业务模块，只调用 `src/codec`，不含任何序列化逻辑 |
| `legacy/user_codec.py` / `legacy/order_codec.py` | 重构前的两份重复实现，冻结保留，仅作差分测试基准 |
| `tests/test_diff.py` | 字节级差分测试：新接口 vs 两份 legacy 实现 |
| `tests/test_roundtrip.py` | 往返一致性：随机记录（含空值/缺失/未知字段）编码再解码逐字段相等 |
| `tests/test_compat.py` | 向前兼容：未知字段保留、缺失字段默认值并记录、坏 magic 拒绝 |
| `tests/fixtures/` | 历史数据样本（v0 无 email、v1 完整、v1 含未知字段、全默认值） |
| `tests/make_fixtures.py` | 用 legacy 代码重新生成 fixtures |

## 线上字节格式（禁止变更）

```
magic(2)="RC" | version(1) | field_count(2, BE) | [tag(1) type(1) len(2, BE) payload(len)]...
```

类型：0x01=int32 0x02=utf-8 字符串 0x03=bool 0x04=float64 0x05=bytes

## 新增一个字段的改动清单（唯一改动点：`src/codec.py` 的 `FIELDS`）

1. 在 `src/codec.py` 的 `FIELDS` 元组**末尾追加**一行
   `Field("字段名", 新tag, 类型常量, 默认值, 编码函数, 解码函数)`；
   新 tag 取未用过的编号，已有条目禁止重排或改 tag。
2. 若现有 5 种类型不够用，才需要在同文件新增一对 `_enc_*` / `_dec_*`
   函数和一个类型常量（新类型编号不得与已有冲突）。
3. 没有了。编码顺序、默认值补齐、缺失记录、未知字段保留、业务模块、
   差分/往返/兼容测试全部自动覆盖新字段，无需改动。

删除字段同理：从 `FIELDS` 移除对应行即可；线上旧数据中的该字段会被
当作未知字段原样保留，不会报错也不会丢失。

## 运行命令

```sh
./run_tests.sh                                  # 全部测试
python3 -m unittest tests.test_diff -v          # 仅字节级差分
python3 -m unittest tests.test_roundtrip -v     # 仅往返一致性
python3 -m unittest tests.test_compat -v        # 仅向前兼容
python3 -m tests.make_fixtures                  # 重新生成历史样本
```
