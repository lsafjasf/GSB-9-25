# GSB-9-25 — appendlog: 追加式日志读写库

纯 Python 3 标准库实现（`os` / `struct` / `zlib` / `mmap`），无第三方依赖。

## 记录格式

小端布局，定长 12 字节头 + 变长负载：

```
+----------+------------------+------------------+==================+
| magic 4B | payload_len u32  | payload_crc u32  | payload (len B)  |
+----------+------------------+------------------+==================+
| "ALG1"   | 负载字节数        | 负载的 CRC32      | 负载本体          |
+----------+------------------+------------------+==================+
```

- `magic`：帧定界，损坏后重新同步（resync）的锚点。
- `payload_len`：负载长度，上限 `MAX_RECORD_SIZE = 64 MiB`，越界即判「长度非法」。
- `payload_crc`：负载的 CRC32（`zlib.crc32`），逐条校验。

## 写入：缓冲区 vs 落盘

```python
import appendlog as al

with al.LogWriter("app.log") as w:
    ack = w.append_batch([b"rec1", b"rec2"])  # 只保证进入 OS 写缓冲
    print(ack.buffered_upto, ack.durable_upto)  # 前者前进，后者不变
    durable = w.sync()                          # fsync，强制落盘
    print(w.durable_upto)                       # 调用方可随时查最后落盘位置
```

- `append_batch()` 返回 `WriteAck(buffered_upto, durable_upto)`，两种确认显式区分。
- `sync()` 执行 `fsync` 并返回最后落盘偏移；`w.durable_upto` 可随时查询。

## 读取与损坏分类

`LogReader(path, mode=...)` 逐条校验，损坏分为四类（前三类为需求要求）：

| 类别 | 常量 | 判定条件 |
|---|---|---|
| 末尾半截 | `TRUNCATED` | 文件尾不足一个完整头，或头声明的负载超出文件末尾（强杀导致的撕裂写） |
| 长度非法 | `INVALID_LENGTH` | 长度字段超过 64 MiB 上限 |
| 校验失败 | `CHECKSUM_MISMATCH` | 记录完整但负载 CRC32 与头部不符（含校验字段被清零、内容被翻转、长度被改小） |
| 帧丢失 | `BAD_MAGIC` | 预期位置找不到 magic（垃圾字节 / 错位），用于跳读模式下重新同步 |

每个损坏都带 `offset` / `end` / `reason`，可精确定位。

## 恢复模式对照表

| | `STRICT`（遇损即停） | `SKIP`（跳过续读） |
|---|---|---|
| 触发损坏时 | 抛出 `CorruptionError`（含偏移与原因） | 记录 `Corruption`，从损坏起点逐字节重同步（不信任被篡改的长度字段，夹在声明跨度内的正常记录会被找回） |
| 损坏后的数据 | 不再读取 | 继续读到文件末尾 |
| 损坏清单 | 只有第一处（异常对象上） | 全部记录在 `ReadResult.corruptions`（含跳过范围） |
| 末尾半截 | 抛异常 | 报告后终止（尾部无可恢复内容） |
| 适用场景 | 强一致性、宁可失败不可错读 | 崩溃恢复、尽量抢救数据 |

```python
# 严格模式
try:
    for rec in al.LogReader("app.log", mode=al.STRICT).iter_records():
        ...
except al.CorruptionError as e:
    print(e.corruption.kind, e.corruption.offset, e.corruption.reason)

# 跳读模式
result = al.LogReader("app.log", mode=al.SKIP).scan()
for c in result.corruptions:
    print(f"跳过 [{c.offset}, {c.end}) {c.kind}: {c.reason}")
```

## 损坏用例集（自测覆盖）

`test_appendlog.py` 共 23 个用例：

- 空文件、单条记录、批量往返、偏移序列、重开后续写
- 写确认语义：buffered/durable 分离、sync 后对齐
- 只有半截记录（头截断 / 负载截断 / 文件整体只有半条）
- 长度字段被篡改（超大值 → `INVALID_LENGTH`；改小 → CRC 失败 + 错位检测）
- 内容字节被翻转 → `CHECKSUM_MISMATCH`
- 校验字段全零 → `CHECKSUM_MISMATCH`
- 垃圾字节夹在记录之间 → `BAD_MAGIC` 并恢复
- 连续多处损坏（校验失败 + 垃圾 + 非法长度 + 尾部半截）逐一识别、偏移单调
- 误判防护：所有篡改样本均不得作为正常记录返回
- 严格模式：首处损坏即抛异常并携带偏移

## 吞吐数据（GB 级）

`python3 bench.py 1024`（1 GiB、1 KiB/条、约 103.7 万条记录，本机实测）：

| 阶段 | 耗时 | 吞吐 |
|---|---|---|
| 写入（批量 append + fsync） | 2.06 s | ≈ 497 MiB/s |
| 读取（mmap 顺序扫描 + 全量 CRC32 校验） | 1.10 s | ≈ 935 MiB/s |

1 074 655 232 字节全部通过 CRC32 校验，无误判。

## 运行命令

```bash
python3 test_appendlog.py   # 自测（unittest，23 个用例）
python3 bench.py 1024       # 吞吐基准，参数为文件大小（MiB），默认 1024
```

## 文件清单

- `appendlog.py` — 库源码（格式、写入器、读取器、损坏分类、两种恢复模式）
- `test_appendlog.py` — 自测与损坏用例集
- `bench.py` — GB 级吞吐基准
