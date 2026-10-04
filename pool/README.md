# 连接池复位修复 —— 归还后不残留超时 / 事务 / 缓冲

纯 Python 3 标准库实现（`socket` / `select` / `socketserver` / `threading`），无第三方依赖。
通过一个真实的回环 TCP 文本协议服务演示：连接被归还后，上一个使用者
残留的超时设置、未结束事务、迟到响应、待发送字节如何影响下一个使用者，
以及修复方案如何逐项复位、失败即销毁。

## 运行方式

```bash
cd pool
python3 reproduce_bug.py          # 复现：朴素池泄漏 vs 修复后干净
python3 -m unittest test_pool -v  # 21 个回归测试
python3 diff_check.py             # 对拍 + 与 golden_diff_data.txt 比对
```

## 文件

- `server.py` — 最小「数据库」TCP 服务（PING/SET/GET/MULTI/EXEC/ROLLBACK/
  SLEEP/STATUS/KILL），每连接独立事务状态；`SLEEP` 用于制造客户端读超时，
  `KILL` 用于模拟对端强制关闭。
- `connection.py` — 客户端 `Connection` 与 `reset()`（逐项复位，失败抛
  `ResetError`）、`ResetReport`（每一步的审计记录）。
- `pool.py` — `ConnectionPool`：借出 / 归还 / 上下文 `lease()` /
  指标（created / destroyed / reset_ok / reset_failed）。
- `reproduce_bug.py` — 复现用例：修复前朴素池的四类泄漏 + 修复后对照。
- `test_pool.py` — 21 个回归测试。
- `diff_check.py` / `golden_diff_data.txt` — 对拍脚本与 golden 数据。

## 连接上可能残留的全部状态（复位清单）

| # | 残留状态 | 来源 | 下一个使用者观察到的现象 | 复位动作 | 复位失败的判断依据 |
|---|---------|------|--------------------------|----------|--------------------|
| 1 | socket 读超时 / 阻塞模式 | 使用者 `set_timeout(0.05)` 后未恢复 | 正常请求莫名 `ReadTimeoutError` | 比较 `sock.gettimeout()` 与默认值，不同则恢复 | 设置失败（`OSError`）→ socket 参数不可控 → 销毁 |
| 2 | 服务端未结束事务 | `MULTI` 后业务异常，未 `ROLLBACK` | `STATUS` 得 `TX=1`，自己的操作并入别人事务，`EXEC` 意外提交 | `STATUS` 查询；`TX=1` 则 `ROLLBACK` 并复查 | STATUS 失败 / 响应畸形 / ROLLBACK 非 `ROLLED BACK` / 复查仍 `TX=1` → 服务端状态不可信 → 销毁 |
| 3 | 读缓冲残留（客户端 `rbuf` + 内核 socket 缓冲） | 请求超时后服务端迟到的响应 | 读到上次的响应，命令-响应错位（`PING`→`SLEPT`） | `select` 探测并 `recv` 丢弃全部残留；发生过超时的连接额外等一个 grace 窗口收迟到尾巴 | recv 返回 b""（对端关闭）或 `OSError` → 销毁 |
| 4 | 待发送字节 `wbuf` | 上次写中断（`send` 失败）残留半条命令 | 若补发则服务端把残命令算到下个人头上；不补发则字节去向不明 | 归还时强制 `sendall` 补发，随后产生的响应由 drain 清掉 | `sendall` 失败 → 字节无法安全送达也无法安全丢弃 → 销毁 |
| 5 | 连接已死（对端 `KILL` / RST / 半关闭） | 服务端强制关闭 | 下个人一发请求就炸 | liveness：`select` + `MSG_PEEK`，读到 `b""` 或 `OSError` 判死 | 判死即销毁，绝不放回池 |

复位顺序（顺序有实际意义，乱序会出错）：

1. **liveness** — 对端已关闭直接判死（带超时的 socket 不能只用 `MSG_DONTWAIT`：
   Python 会先按内部超时等待，必须先 `select(0)`）；
2. **timeout** — 恢复默认超时与阻塞模式，让后续步骤的 IO 行为可预期；
3. **flush** — 补发待发送字节（补发会引发服务端响应，下一步统一清掉）；
4. **drain** — 清空读缓冲（含迟到响应）；
5. **transaction** — 缓冲已空，此时 `STATUS` 的响应必须严格是 `TX=0/1`，
   畸形即说明流错位；`TX=1` 则 `ROLLBACK` 并复查；
6. **ping** — 端到端终检：`PING` 必须恰好收到 `PONG`，否则流已错位。

**失败处理总原则**：任何一步抛 `ResetError`，连接池都调用 `close()` 销毁连接、
不放入空闲队列，并递减存活计数（下次借出时懒创建补齐）。判断依据：复位后连接
必须与「新建连接」不可区分；只要有任何一步无法确认这一点，继续复用就会把
残留状态泄漏给下一个使用者，销毁是唯一安全选择。

## 覆盖的归还情形

- **正常归还**：复位成功，同一连接对象被复用（`test_normal_return_reuses_connection`）。
- **归还时业务抛异常**：`with pool.lease()` 的 `finally` 仍走复位路径，异常原样上抛，
  连接复位后可复用（`test_return_with_exception_still_resets`）。
- **部分复位失败**：注入 drain 失败（`FaultyConnection`）与真实失败（残留事务 +
  对端 KILL），连接均被销毁、计数正确、下一次借到新连接
  （`test_partial_reset_failure_destroys_connection`、
  `test_reset_failure_on_dirty_connection_destroys`，另有每一步的失败用例）。
- **连接被强制关闭**：`KILL` 后归还，liveness 判死销毁，不进池
  （`test_killed_connection_is_discarded_not_reused`）。
- 并发 8 线程 × 20 次借还无串话（`test_concurrent_lease_no_cross_talk`）。

## 对拍数据

`diff_check.py` 对 6 个场景（clean / timeout / transaction / buffer /
全部残留叠加 / killed）执行同一套 11 项探针序列（PING、STATUS、未读字节数、
超时值、SET/GET、MULTI→SET→ROLLBACK 的事务语义），复用连接与全新连接的
输出逐字段相等；结果固定保存在 `golden_diff_data.txt`，回归时自动比对。
其中 killed 场景验证「销毁后懒创建的新连接」与 fresh 完全一致，
池指标为 `created=2 destroyed=1 reset_ok=11 reset_failed=1`。
