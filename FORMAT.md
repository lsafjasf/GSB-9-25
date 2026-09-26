# 压缩格式说明与迁移指南（v1 → v2）

## 1. 背景：历史实现（v1）的五个缺陷

v1 见 `mini_compress/legacy.py`，帧头为 `LC1` + uint16 原始长度，载荷是自研 LZSS token 流。
五个生产问题及根因：

1. **压缩后反而变大**：没有"原样存储"模式，任何数据都走 LZSS；不可压缩数据每个字节至少膨胀为 2 字节 token。
2. **特定结尾解压少末尾字节**：当匹配恰好延伸到输入末尾时，编码器把匹配长度减 1 后直接结束编码，末尾字节不再以字面量补回；解码端只按 token 流重建，于是丢字节。
3. **同输入结果不同**：等长匹配用 `hash()` 加盐随机挑选，`PYTHONHASHSEED` 逐进程变化，输出随之变化。
4. **截断数据静默成功**：头部只有"原始长度"，没有 token 字节数与校验；解码端只遍历现存 token 然后按原始长度切片，缺 token 时返回部分数据且不报错。
5. **错误不可区分**：所有失败路径统一抛 `RuntimeError("bad data")`，头部非法、截断、载荷损坏无法区分。

## 2. 修复后格式（v2）

v2 见 `mini_compress/compress2.py`，魔数 `MC2`，与 v1 互不冲突。固定 17 字节大端头部：

| 偏移 | 长度 | 字段 |
|---:|---:|---|
| 0 | 3 | magic，固定 `MC2` |
| 3 | 1 | version，固定 `0x02` |
| 4 | 1 | flags，bit0 为存储模式，其余保留位必须为 0 |
| 5 | 4 | original_len，原始数据长度 |
| 9 | 4 | payload_len，载荷精确字节数 |
| 13 | 4 | crc32，载荷的 CRC-32/IEEE 校验 |
| 17 | … | payload |

两种模式（flags bit0）：

- `STORED`（0）：载荷 = 原始数据，原样保留。解码端按该模式识别。
- `DEFLATE`（1）：载荷是 raw DEFLATE 流（RFC 1951，`wbits=-15`，固定 level 9）。

缺陷修复对应关系：

1. **膨胀**：编码器先试 DEFLATE，只有 `len(deflated) < len(原始)` 才采用，否则明确写 `STORED`。载荷长度永远 ≤ 原始长度；最坏总开销恒为 +17 字节。
2. **末尾丢字节**：改用标准 zlib，头部携带 original_len，解码后强制校验长度；`test_roundtrip` 对全部分布断言逐字节相等。
3. **确定性**：无 hash/随机选路，固定 level；同一输入两次压缩字节完全一致，且跨 `PYTHONHASHSEED` 进程一致。
4. **截断**：payload_len 给出精确边界。头部不全 / 载荷不足 → `TruncatedError`；尾部多字节 → `HeaderError`。校验全部通过前不返回任何数据。
5. **可区分错误**：独立异常类型，且全部继承 `CompressionError`：

| 异常 | 触发条件 |
|---|---|
| `TruncatedError` | 不足 17 字节头部；声明的 payload_len 没到 |
| `HeaderError` | magic/version 错误、保留 flag 非零、STORED 长度自相矛盾、尾部多余字节 |
| `ChecksumError` | 载荷 CRC-32 与头部不一致 |
| `CorruptPayloadError`（继承 `ChecksumError`） | CRC 正确但 DEFLATE 无法解码或解码长度 != original_len |

注意分类顺序：先验长度边界，再验 CRC，最后解码载荷，保证"截断"不会被误报成"校验失败"。

## 3. 不同数据分布下的体积对比

`python3 bench_size.py` 实测（括号内相对原始数据变化率；v2 含固定 17 字节头）：

| 分布 | raw | v1 | v2 |
|---|---:|---:|---:|
| 空 | 0 | 5 (+500%) | 17 STORED |
| 1 字节 | 1 | 7 (+600%) | 18 STORED |
| 2 字节 | 2 | 9 (+350%) | 19 STORED |
| 重复字节 ×64 | 64 | 15 (−76.6%) | 23 DEFLATE (−64.1%) |
| 重复字节 ×1000 | 1000 | 119 (−88.1%) | 28 DEFLATE (−97.2%) |
| 周期数据 (ab×500) | 1000 | 121 (−87.9%) | 29 DEFLATE (−97.1%) |
| 高位字节斜坡 ×4 | 512 | 305 (−40.4%) | 165 DEFLATE (−67.8%) |
| 英文文本 | 540 | 145 (−73.1%) | 68 DEFLATE (−87.4%) |
| 伪随机 32B | 32 | **69 (+115.6%)** | 49 STORED（仅 +17 头） |
| 伪随机 200B | 200 | **405 (+102.5%)** | 217 STORED（+8.5%） |
| 伪随机 1000B | 1000 | **2005 (+100.5%)** | 1017 STORED（+1.7%） |

结论：v1 在随机数据上稳定膨胀一倍以上；v2 对此类数据走 STORED，只承担固定 17 字节头开销，数据越长相对开销越小。小块（< 17 字节）即使是 STORED 也会有绝对 +17 的固定成本，这是为可校验、可截断检测付出的代价；对极小且追求极致体积的场景，可由调用方自行决定不入库。

## 4. 历史数据迁移

v2 与 v1 magic 不同，属于**带版本号的并存格式**，不是原地修改：

- **新写入**：直接使用 v2（`compress2.compress`）。
- **读取历史**：`legacy.inspect()` 结构化读取 v1，返回 `("ok", data)` / `("truncated", data)` / `("unrecoverable", data)`；`migrate.py` 按帧迁移。
- **可恢复性的事实边界**：
  - v1 帧若原本就能无损往返（没有触发缺陷 #2），可无损重编码为 v2；
  - 已经被缺陷 #2（末尾丢字节）或 #4（截断）破坏的 v1 帧，**丢失的字节在帧里不存在，任何工具都无法重建**，迁移工具会明确跳过并在报告里标记 `skipped:truncated` / `skipped:unrecoverable`，退出码 1，绝不把残缺数据伪装成成功；
  - v1 没有 token 长度字段，因此当"提前结束的缺陷帧"恰好是流中最后一帧时，它与"被截断的帧"无法区分，工具按截断拒绝，避免猜测。
- **v2 帧**：迁移时逐字节保留（幂等），并先做一次完整解码验证。

所以历史数据**不需要强制迁移**即可与 v2 并存；若要统一格式，运行：

```bash
python3 migrate.py old_frames.bin > new_frames.bin   # 或 stdin 输入
```

## 5. 运行命令

```bash
# 五类缺陷的稳定复现（测试断言的是 v1 的缺陷行为）
python3 -m unittest tests.test_reproduce -v

# 修复版回归：往返一致、确定性、STORED 选路、截断/校验/头部错误分类、迁移
python3 -m unittest tests.test_regression -v

# 全部测试
python3 -m unittest discover -s tests -v

# 体积对比
python3 bench_size.py

# 历史数据迁移
python3 migrate.py old_frames.bin > new_frames.bin
```

仅使用 Python 3 标准库（`zlib`、`unittest`、`hashlib` 之外无第三方依赖；CRC 由 `zlib.crc32` 提供）。
