# field_crypto — 字段级加密库（纯标准库）

敏感字段加密落盘：版本化密钥、随机 IV、分块完整性校验（可定位篡改位置）、
AAD 关联绑定（防跨记录复制）、可中断/可重入的密钥轮换。仅依赖 Python 3 标准库。

## 密文格式

```
MAGIC 4B | KEY_VERSION 4B | IV 16B | AAD_TAG 32B | PLAINTEXT_LEN 8B
| CHUNK_SIZE 4B | NUM_CHUNKS 4B | HEADER_TAG 32B | [CHUNK_CT, CHUNK_TAG 32B] * NUM_CHUNKS
```

- 加密：HMAC-SHA256 派生的 CTR 风格密钥流（标准库无 AES 的替代构造）。
- 完整性：头部有独立的 HEADER_TAG，解密时先校验头部、再逐块校验密文体；
  每个 64 KiB 分块独立 encrypt-then-MAC，篡改可定位到区域（头部/分块号）与字节偏移。
- 每条记录固定开销 136 B（104 B 头 + 32 B 分块标签）。

## 快速开始

```python
import field_crypto as fc

store = fc.KeyStore()
store.add(1, fc.generate_key())          # 第一个版本自动成为 active

token = fc.encrypt(store, b"ssn:110...", aad=b"user:42")   # aad 绑定记录主键
plain = fc.decrypt(store, token, aad=b"user:42")

# 轮换：旧密钥保留解密，新写入用 v2
store.add(2, fc.generate_key())
store.set_active(2)
new_token = fc.rotate(store, token, aad=b"user:42")        # 单条重加密（幂等）
fc.reencrypt_batch(store, records, "field", aad_of=lambda r: f"user:{r['id']}".encode())
```

## 错误类型（可区分）

| 异常 | 场景 |
|---|---|
| `KeyMissingError` | 密钥库无 active 密钥 |
| `UnknownKeyVersionError` | 密文引用的密钥版本不存在（`.version`） |
| `InvalidFormatError` | MAGIC 不符，非本库密文 |
| `TruncatedCiphertextError` | 密文被截断（`.expected` / `.actual`） |
| `LengthMismatchError` | 实际长度与头部声明不符（含尾部多余字节） |
| `IntegrityError` | 密文被篡改（`.region` 为 `"header"`/`"chunk"`，`.chunk_index` / `.byte_offset` 指出位置） |
| `AssociatedDataMismatchError` | AAD 不匹配：密文被复制到别的记录 |

## 密钥管理说明

- 密钥以 `(version, key)` 形式登记进 `KeyStore`；生产环境应把密钥放在 KMS/机密
  管理服务中，启动时注入，绝不要与密文同库存储。
- 轮换流程：`add(new_version, key)` → `set_active(new_version)` → 后台任务用
  `reencrypt_batch` 逐步重加密。旧版本密钥**必须保留**直到确认所有引用它的密文
  都已重加密（可用 `peek_key_version` 扫描），之后才能从密钥源中下线。
- 重加密以单条记录为原子单位：每条记录任一时刻要么是旧密文要么是新密文，
  进程中断后重跑即可（已轮换的记录自动跳过，幂等可重入）。
- `generate_key()` 使用 `os.urandom`（CSPRNG）；IV 每条密文随机生成，
  同一明文两次加密结果不同。

## 运行

```bash
python3 -m unittest test_field_crypto -v   # 自测（篡改/跨记录复制/轮换/边界）
python3 bench.py                            # 10 万条记录性能基准
```

## 性能（CPython 3.12，x86_64，10 万条 ~120 B 记录）

| 操作 | 耗时 | 吞吐 |
|---|---|---|
| 加密 | 0.99 s | ~101,000 ops/s（9.9 µs/条） |
| 解密 | 0.92 s | ~108,000 ops/s（9.2 µs/条） |
| 轮换重加密 | 1.89 s | ~53,000 ops/s（解密+加密） |
