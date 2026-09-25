# 严格十六进制 / Base64 编解码库

纯 Python 3 标准库实现，**不调用** `binascii`、`base64`、`bytes.hex`、`bytes.fromhex`
等自带编解码功能，位运算与查表全部手写。用途：密钥、签名参数在文本格式中的精确往返。

## 文件

- `strict_hex.py` — 十六进制编解码
- `strict_b64.py` — Base64 编解码（RFC 4648）
- `selftest.py` — 标准向量对拍 + 严格解码用例 + 全边界往返
- `bench.py` — 超长输入性能基准

## 运行命令

```sh
python3 selftest.py     # 自测，全部通过退出码 0
python3 bench.py        # 性能基准（默认最大 16 MiB）
python3 bench.py 64     # 性能基准（最大 64 MiB）
```

## 行为约定

### 十六进制
- 编码：输出固定小写。
- 解码：输入大小写均可；`0x`/`0X` 前缀默认**拒绝**（位置 0 报错），
  显式 `allow_prefix=True` 才剥离；奇数长度、非法字符（含非 ASCII）均报
  `HexDecodeError` 并给出字符下标。

### Base64（三个独立开关，绝不自动猜测）
- `urlsafe`：标准字母表 / URL 安全字母表，必须显式二选一；混用字符按非法字符报错。
- `require_padding`：`True` 要求 RFC 4648 规范填充（缺失报错）；
  `False` 要求无填充（出现 `=` 报错）。
- `allow_newlines`：`False` 时 `\n`/`\r` 按位置报错；`True` 时剥离后解码。

### 严格性
非法字符、长度非法（模 4 余 1、含填充总长非 4 倍数）、填充错误（中间出现、
超过 2 个）、填充缺失、**非零填充位**（RFC 4648 §3.5 规范性检查）全部抛
`B64DecodeError` 并给出位置，不做任何自动纠正或忽略。

## 性能设计（避免的低效拼接）

- 编码用「列表收集 + 一次 `str.join`」，不在循环里 `+=` 字符串；
- Base64 编码主循环用 12bit→2 字符预计算表，每 3 字节只查表 2 次；
- 解码按输出长度**预分配 `bytearray`** 并按下标写入，不用 `bytes` 拼接；
- 非法字符校验用 `str.translate`（C 速度）一次筛出，合法输入零 Python 循环；
- 全程单遍 O(n)，64 MiB 输入吞吐与 1 MiB 基本持平，无平方级退化。
