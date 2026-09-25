# 审计日志哈希链库（audit_chain）

纯 Python 3 标准库实现，证明审计记录未被事后修改/删除，并指出首处不一致的位置与改动类型。

## 文件

| 文件 | 说明 |
|---|---|
| `audit_chain.py` | 库：记录格式、追加、锚点、完整/增量校验 |
| `test_audit_chain.py` | 自测（18 个用例，unittest） |
| `tamper_cases.py` | 篡改用例集 + 检出结果演示 |
| `bench.py` | 十万条性能测试 |

## 运行命令

```bash
python3 -m unittest test_audit_chain -v   # 自测
python3 tamper_cases.py                   # 篡改用例集与检出结果
python3 bench.py                          # 性能（默认 100000 条，可传 N）
```

## 记录格式与可重现性

每条记录 `{alg, seq, prev, data, hash}`：

- `hash = sha256(canonical_json({seq, prev, data}))`；
- 序列化固定为 `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)` + UTF-8 —— 字段顺序、分隔符、编码均固定，任何机器上重算结果一致；
- `seq` 从 0 连续编号，`prev` 指向前条摘要（首条为 64 个 `0` 的创世值）；
- 持久化为 JSONL（一行一条），只追加（O_APPEND），历史行从不重写。

## 四类问题的区分

`verify()` 返回 `VerifyResult(ok, error, position, detail, checked)`，`error ∈ {modified, inserted, deleted, truncated}`，`position` 为首处不一致下标：

1. **modified**：存储摘要与内容重算结果不符；或序号连续但链接断裂（前条被替换并重算了摘要）。
2. **inserted**：后一条记录的 `prev` 绕过当前记录；或序号重复/回退（复制记录到别处、拼接外来链均属此类）。
3. **deleted**：序号出现前向空洞，或 `prev` 越过前一条记录。
4. **truncated**：链自身自洽但短于可信锚点 `Anchor(count, tail_hash)` —— 截断在密码学上只能依靠链外锚点检出，锚点应定期签名并外置保存。

同理，若攻击者改动内容后**重算整条链**，局部校验必然通过，只有锚点比对能检出（见用例集最后一条）。

## 追加原子性

`append()` 以 O_APPEND 打开文件，整行**单次 write** 后 `flush + fsync`：POSIX 保证 O_APPEND 写定位原子，不会与历史数据交错；崩溃最坏留下一条撕裂尾行，加载时以 `CorruptStoreError` 明确检出，截掉该行即可恢复，历史摘要绝不被覆盖。追加返回前做 O(1) 自检（重算本条摘要 + 校验与前条的链接），保证追加后立即 `verify()` 通过。

## 性能（本机实测，Python 3.12，100,000 条）

| 操作 | 耗时 |
|---|---|
| 构建（含哈希与逐条自检） | ~0.62 s（≈16 万条/s） |
| 完整校验（无锚点） | ~0.25 s（≈40 万条/s） |
| 完整校验（带锚点比对） | ~0.25 s |
| 增量校验（任意位置 1000 条） | ~2.5 ms |

**增量校验**：支持。`verify(start, stop)` 以 `records[start].prev` 为区间锚，只校验 `[start, stop)` 的内部一致性，复杂度 O(区间长度)，与全链长度无关；区间与全链的信任关系配合 `Anchor` 建立。

## 覆盖用例

空链、单条记录、中间记录被替换、复制整条记录到别处、两条链拼接，以及内容修改、条目插入/删除（含首条）、链尾截断、整链重算、持久化往返、撕裂尾行等，见 `test_audit_chain.py`（18 例全部通过）与 `tamper_cases.py` 输出。
