# mini_compress — 可变小块数据压缩（缺陷修复交付）

纯 Python 3 标准库实现（`zlib` 仅用于 CRC-32，压缩算法为自实现的
确定性 LZSS）。修复了现网五类问题：压缩后膨胀、末字节丢失、同输入结果
不确定、截断被当成功、错误原因不可区分。

## 目录结构

```
mini_compress/
  __init__.py            包入口与公开 API
  mini_compress.py       修复后的 MCP1 实现（compress/decompress + 3 类错误）
  legacy_buggy.py        旧 MC0 实现（保留用于复现与历史识别）
tests/
  test_repro_legacy.py   五类缺陷的稳定复现用例（针对旧实现）
  test_mini_compress.py  往返/确定性/存储模式/错误分类/截断损坏模糊测试
benchmark_sizes.py       不同数据分布下的体积与耗时对比（含修复前后编码器对照）
FORMAT.md                格式规范、修复前后差异、迁移说明
```

## 运行命令

```bash
# 全部测试（5 个复现 + 25 个回归）
python3 -m unittest discover -s tests -v

# 只看五类缺陷复现
python3 -m unittest tests.test_repro_legacy -v

# 只看修复回归
python3 -m unittest tests.test_mini_compress -v

# 体积与耗时对比
python3 benchmark_sizes.py
```

无第三方依赖，Python 3.8+ 即可（开发环境 3.12）。

## 快速使用

```python
from mini_compress import compress, decompress
from mini_compress import HeaderError, TruncatedError, ChecksumError

frame = compress(b"abcabcabc" * 10)
assert decompress(frame) == b"abcabcabc" * 10
```

格式细节、与旧格式的逐项差异、历史数据迁移策略见 `FORMAT.md`。
