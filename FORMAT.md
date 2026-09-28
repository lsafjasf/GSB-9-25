# 压缩格式与迁移说明

## 1. 新格式：MCP1（修复后）

所有整数均为大端序。

### 1.1 帧头（固定 13 字节）

| 偏移 | 长度 | 字段 | 说明 |
|------|------|------|------|
| 0 | 4 | magic | 固定 `4D 43 50 31`（ASCII `MCP1`） |
| 4 | 1 | mode | `0x00` = RAW（原样存储），`0x01` = LZ |
| 5 | 4 | orig_len | 解压后数据长度，uint32 |
| 9 | 4 | crc32 | **解压后数据** 的 CRC-32（`zlib.crc32`） |
| 13 | 变长 | payload | 见下 |

### 1.2 存储模式

- **mode = 0（RAW）**：payload 就是原始数据，逐字节一致。
- **mode = 1（LZ）**：payload 是如下标记流。

### 1.3 LZ 标记流（确定性 LZSS）

| 标记 | 编码 | 含义 |
|------|------|------|
| 字面量 | `tag` ∈ `0x01..0x3F`，后跟 tag 个字节 | 直接拷贝 1..63 字节 |
| 回溯 | `tag` ∈ `0x80..0x8F` + 4 字节 | 28 位 offset = `(tag&0x0F)<<24` 与后续 3 字节（u24be）拼接；length = 第 5 字节 + 3 |
| 结束 | `0x00` | 流结束（且其后不得再有字节） |
| 保留 | `0x40..0x7F`、`0x90..0xFF` | 非法，遇到即 `HeaderError` |

回溯标记完整布局（共 5 字节）：

```
byte0: 0x80 | ((offset >> 24) & 0x0F)
byte1: (offset >> 16) & 0xFF
byte2: (offset >>  8) & 0xFF
byte3: offset & 0xFF
byte4: length - 3          # length 范围 3..258
```

offset 为相对当前已输出末尾的距离（1..0x0FFFFFFF，28 位），允许重叠拷贝
（如 RLE：offset=1 时逐字节复制、允许源在同一次拷贝中前进）。

### 1.4 确定性保证

编码器在等长匹配中**总是选择最近的 offset**（严格 `>` 更新），不使用
任何模块级/调用间状态，也不依赖 dict 或 set 的迭代顺序。因此同一输入
在同进程多次调用、跨进程、不同 Python 哈希种子下产出完全相同的字节
（测试 `test_deterministic_across_fresh_processes` 实际跨进程断言）。

匹配查找分两级：先做一次带候选数上限（256）的由近及远回扫——高匹配
密度数据在这里就已决出最长匹配，零索引开销；只有当一次查询扫描超过
上限仍未决出结果时，才构建 3 字节前缀的哈希链索引（`head`/`prev`
数组）并在本流后续位置一直使用。不同前缀不可能产生长度 ≥3 的匹配，
因此链上候选与回扫候选完全等价，仍按由近及远、严格 `>` 更新，发出的
标记流与完整回扫**逐字节一致**（测试
`test_encoder_matches_full_scan_byte_for_byte` 覆盖两条路径），但低
匹配密度数据（本应走 RAW 的不可压缩输入）从平方级降为期望线性时间。
哈希函数是固定常数乘法混合，与进程哈希种子无关。

### 1.5 模式选择策略（不膨胀）

```
若 len(LZ payload) < len(data)  →  mode=LZ（严格更小才压缩）
否则                            →  mode=RAW（平局也走 RAW）
```

帧大小上界恒为 **原始长度 + 13**（见 `benchmark_sizes.py`）。

### 1.6 解压端错误分类（不输出半成品）

解码全程写入本地缓冲，**只有**长度、语法、CRC 全部通过后才返回；任何
失败都不会返回部分数据。三类错误互为兄弟异常（共同基类 `MCPError`）：

| 异常 | 触发条件 |
|------|----------|
| `HeaderError` | magic 错误；mode 未知；保留标记；offset=0 或越过流起点；匹配超出声明长度；END 后有尾随字节；声明长度超安全上限（64 MiB） |

压缩入口与解压入口使用**同一个** `MAX_DECODED_SIZE`（64 MiB）上限：
超过上限的输入在 `compress` 阶段即以 `HeaderError` 拒绝，不会产生
"压得出来却解不开"的帧。
| `TruncatedError` | 帧短于 13 字节头；字面量/回溯标记不完整；LZ 流缺 END；声明长度与实际解码长度不符；RAW 长度不符 |
| `ChecksumError` | 结构完整、长度一致，但 CRC-32 不匹配 |

判定顺序保证错误类型稳定：先信封（magic/mode/头长），再语法与长度，
最后 CRC。

## 2. 旧格式：MC0（`legacy_buggy.py`，保留仅为复现与识别）

帧布局：`"MC"` + uint16 原长度 + 标记流。标记：`0x00` 结束；
`0b01nnnnn` 字面量（1..31）；`0b1ooooooo` + 1 字节长度附加位（回溯，
offset 1..127，length 3..34）。无 CRC，原长度字段解码时不校验。

### 2.1 五类缺陷与修复对照

| # | 缺陷（MC0） | MCP1 修复 |
|---|-------------|-----------|
| 1 | 只有压缩一种模式，随机/极小数据膨胀 | 显式 RAW 模式，严格更小才压缩，上界 orig+13 |
| 2 | 编码循环上界 `len-1`，末字节可能丢失；重叠回溯还会错拷 | 无 off-by-one；重叠回溯逐字节复制；orig_len 与 CRC 双校验 |
| 3 | 模块级轮转状态跨调用泄露，平局选择随调用次数变化 | 无共享状态；等长匹配固定选最近 offset；跨进程测试 |
| 4 | 截断时返回已解码前缀并当作成功 | 全量解码到本地缓冲，任一检查失败即抛异常，绝不返回部分结果 |
| 5 | 所有错误都是 `Exception("bad data")`，且无校验 | `HeaderError` / `TruncatedError` / `ChecksumError` + CRC-32 |

## 3. 历史数据迁移

**不能透明迁移，也不需要批量紧急迁移。** 原因：

1. MC0 没有 CRC，且其编码缺陷会主动丢弃末字节——历史帧中**已丢失的
   字节无法从帧本身恢复**，任何“自动解压再压”的流程都会把损坏数据
   固化成看似有效的 MCP1 帧。
2. 但旧格式对**没有触发缺陷的帧**仍然可读，可用
   `mini_compress/legacy_buggy.py` 尽力读取。
3. 两种格式按 magic 区分：MCP1 = `MCP1`，MC0 = `MC`（第二个字节为
   `C` 而非 `P`），不会误认。

推荐迁移流程（有权威原始数据时）：

```python
from mini_compress import compress
from mini_compress.legacy_buggy import decompress as legacy_decompress

def reencode(old_frame, source_bytes):
    # 必须由调用方提供权威原始数据（数据库/备份/上游系统）。
    frame = compress(source_bytes)
    # 可选健全性核对：仅当旧帧恰好无损时才与 legacy 输出一致
    try:
        if legacy_decompress(old_frame) != source_bytes:
            log("旧帧原本就有损，已按权威源重写")
    except Exception:
        log("旧帧不可解析，已按权威源重写")
    return frame
```

- 读路径可长期双格式共存：先看 magic，`MCP1` 走新解码器，`MC` 走旧
  解码器并标记数据为“未校验/可能有损”。
- 新写入一律使用 MCP1；迁移只需在数据被再次写入时惰性进行，无需停机。
