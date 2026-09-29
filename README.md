# 审计日志哈希链库（Python 3，仅标准库）

用 SHA-256 哈希链证明审计记录未被事后修改/删除，并区分**内容修改、插入、删除、链尾截断**四类问题，
给出首处不一致的位置。

## 文件

| 文件 | 作用 |
| --- | --- |
| `audit_chain.py` | 库：固定摘要/序列化规范、`AuditLog` 追加链、`verify_records` 校验 |
| `test_audit_chain.py` | 13 个自测用例（空链/单条/替换/复制/拼接/截断/增量等） |
| `tamper_cases.py` | 篡改用例集与检出结果演示 |
| `benchmark.py` | 十万条记录的构建/校验/增量/追加性能基准 |

## 运行

```bash
python3 test_audit_chain.py    # 自测
python3 tamper_cases.py        # 篡改用例集与检出结果
python3 benchmark.py 100000    # 性能数据（默认就是 10 万）
```

## 记录格式与摘要规范（固定、可重现）

每条记录字段：`seq`（序号，0 起）、`prev`（前一条记录摘要 hex）、
`content_hash`（自身内容摘要 hex）、`digest`（本条记录摘要 hex）、`content`（业务内容，任意 JSON 值）。

- 内容编码：`json.dumps(content, sort_keys=True, separators=(",",":"), ensure_ascii=False)` → UTF-8。
  键序统一排序，因此对象键书写顺序不影响摘要。
- 内容摘要：`SHA256("AUDITCHAIN/v1|C|" + canonical_content)`（域分离前缀）。
- 记录摘要：`SHA256("AUDITCHAIN/v1|R|" + seq(8 字节大端) + prev(32 B) + content_hash(32 B))`。
  摘要覆盖 `seq/prev/content_hash`，任何一个字段被改都无法通过重算。
- 落盘：JSONL，每行一条，键顺序固定，UTF-8 + LF。空链链头为 `00×32`（GENESIS）。

## 使用

```python
from audit_chain import AuditLog

log = AuditLog("audit.jsonl")
log.append({"op": "transfer", "from": "alice", "to": "bob", "amount": 100})

print(log.verify())                      # 全量校验 -> VerifyResult
length, head = log.checkpoint()          # 保存可信检查点 (长度, 链头摘要)
print(log.verify(expected_head=head, expected_len=length))
# 提供可信 (条数, 链头) -> 区分截断/整链重算与检查点之后的正常追加
print(log.verify_from(50000))            # 从任意位置增量校验
```

## 四类问题的判定与首处位置

校验按记录顺序逐条进行，**遇到的第一处不一致即返回**（`VerifyResult.error/position/detail`）：

1. **content_modified**：重算的 `content_hash` 或 `digest` 与存储值不符 → 位置为该条序号；
   若攻击者重算了被改条目的摘要但没有能力重算整条后续链，相邻 `prev` 衔接断裂，
   定位到断裂处并报 content_modified。
2. **entry_inserted**：出现序号回退/重复（外来或复制条目），或某条的后继绕过它直接链接其前驱。
3. **entry_deleted**：出现序号跳变（期望 `i` 实际更大），或某条 `prev` 越过前驱指向再前一条。
4. **truncated**：内部校验全部通过，但现存条数少于可信检查点 `expected_len` →
   位置 = 现存长度。截断/整链删除在密码学上只能靠外部可信锚点检出，这是检查点机制的用途。
   检查点由 `(条数, 链头)` 共同构成：条数够且检查点位置处摘要一致时，检查点之后的
   正常追加不误报；条数够但锚定位置摘要不符，说明检查点之前的历史被重算改写，
   报 content_modified，位置 = 检查点锚定的记录序号。只给 `expected_head` 不给
   `expected_len` 时退化为比较最终链头，无法区分截断与正常变长。

## 追加的原子性与不可重写

- 追加只通过 `O_WRONLY|O_CREAT|O_APPEND` 打开文件，从不 seek、从不重写既有字节。
  POSIX 下 O_APPEND 的“偏移更新 + 写入”对并发写者是不可分的；整行在**单次 `write(2)`**
  调用中写入，并发追加不会交错产生半行。
- 写入后 `fsync` 落盘才应答（持久性）。
- 追加完成后立即做 O(1) 校验：新记录的 `prev` 必须等于旧链头，且 `digest` 重算一致，
  不一致则 `assert` 失败且该条不可信（不会静默成功）。
- 崩溃语义：若崩溃发生在一次行写入中途，最后一行残缺/非 JSON，加载时不会抛解析异常，
  `verify` 在该尾部位置报 content_modified（detail 含文件行号）；去掉残缺末行后链即恢复
  一致（未确认数据不入账）。

## 增量校验

支持。`verify_records(records, start, known_prev)` / `log.verify_from(start)` 只扫描
`records[start:]`，`known_prev` 为 `records[start-1]` 的可信摘要（来自检查点；`start=0`
时为 GENESIS）。后半段的任意四类篡改同样能被定位。

## 篡改用例检出结果（本机实测）

```
用例                              检出类型             位置
0. 未篡改（对照组）                 ok                 -
1. 修改第5条内容                   content_modified   5
2. 修改第5条并重算其摘要            content_modified   5   （第5/6条间链断裂）
3. 在位置6插入伪造条目              entry_inserted     7   （序号回退）
4. 把第2条完整复制到位置9           entry_inserted     9   （序号重复）
5. 删除第8条                       entry_deleted      8   （序号跳变）
6. 截断链尾（有可信链头）            truncated          9
7. 截断链尾（无可信链头）            ok                    （内部自洽，需检查点）
8. 另一条链拼接到链尾               entry_inserted     12  （拼接点）
```

## 性能数据（本机实测，100,000 条，约 334 B/条，31.9 MiB）

- 全量校验（内存）：**0.27 s**，约 36.6 万条/s。
- 全量校验（含从磁盘加载的冷启动）：**0.96 s**。
- 增量校验（后 5 万条）：**0.13 s**，复杂度 O(N−start)。
- 追加接口（含每次 `fsync`）：约 1.35 ms/条（瓶颈是 fsync；批量场景可放宽持久化节奏）。

## 安全边界

攻击者若掌握写权限并能从篡改点起**重算其后全部记录摘要**，纯哈希链内部无法区分，
必须借助外部可信链头/检查点（`expected_head`）或外部签名/时间戳——本库的
truncated 检测即为此设计。
