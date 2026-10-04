# 连接池归还复位修复（Python 3，仅标准库）

问题：连接归还时带着上次请求的**未结束事务、语句超时、未读结果缓冲、
autocommit 开关、会话变量、预编译语句、临时表**直接放回池，
下一个使用者借到同一条物理连接，收到莫名其妙的结果。

## 文件

| 文件 | 作用 |
| --- | --- |
| `simconn.py` | 类 DB-API 连接模拟器，显式建模 8 类残留状态，支持故障注入（`raise` / `noop`）与 `force_close` |
| `pool.py` | 修复后的连接池：归还时逐项复位，任何一步失败即销毁连接 |
| `reproduce_bug.py` | 复现用例：旧行为（`legacy_bug_mode=True`）泄漏状态 vs 修复后行为 |
| `crosscheck.py` | 对拍：7 种脏工作负载下，“复用连接”与“全新连接”的快照+行为签名一致 |
| `test_pool_reset.py` | 回归测试（unittest，22 个用例） |

## 运行方式

```bash
cd pool_fix
python3 reproduce_bug.py                 # 复现 bug + 修复对照（退出码 0）
python3 crosscheck.py                    # 对拍：复用连接 vs 新连接（7/7）
python3 -m unittest test_pool_reset -v   # 回归测试（22 个用例）
```

## 连接上可能残留的全部状态（复位清单）

`pool.py` 的 `RESET_STEPS` 是唯一事实来源，测试
`TestResetChecklistIntegrity` 保证清单覆盖 `SimConnection.SNAPSHOT_KEYS`
全部状态、顺序安全且实现不偏离清单。

| # | 步骤 | 残留状态 | 复位动作 | 校验（期望） | 真实系统对应 |
| --- | --- | --- | --- | --- | --- |
| 1 | `drain_results` | 未读结果集缓冲 | 排空剩余行 | `unread_rows == 0` | 取消请求 / fetch 到 EOF，清协议流 |
| 2 | `rollback_txn` | 未结束事务（含 aborted 事务） | `ROLLBACK` | `in_transaction is False` | `conn.rollback()`（PG aborted 事务只能 ROLLBACK） |
| 3 | `abort_cleared` | aborted 标志残留 | （随第 2 步） | `txn_aborted is False` | `InFailedSqlTransaction` 状态解除 |
| 4 | `timeout` | 语句/查询超时 | 重置超时 | `statement_timeout_ms == 0` | `RESET statement_timeout` / `SET lock_wait_timeout=DEFAULT` |
| 5 | `autocommit` | autocommit 开关被改 | 恢复默认 | `autocommit is True` | `SET AUTOCOMMIT=1` |
| 6 | `session_vars` | 会话变量 / GUC | 全部重置 | `session_vars == {}` | `RESET ALL` / 会话级 SET 清理 |
| 7 | `prepared_stmts` | 服务端预编译语句 | `DEALLOCATE ALL` | `prepared_statements == set()` | PG `DEALLOCATE ALL`；MySQL 无服务端预编译则跳过 |
| 8 | `temp_tables` | 会话级临时表 | 逐个 DROP | `temp_tables == set()` | DROP 临时表，或显式 `DISCARD TEMP` |
| 9 | `health_check` | 连接是否还活着 | `ping()` 且读快照 | `ping() is True` 且 `open is True` | 归还后/借出前 `SELECT 1` |

顺序依据：**先排空协议缓冲**（否则缓冲里的错误/行会污染后续命令的响应）
→ **回滚事务**（aborted 事务里除 ROLLBACK 外的命令都会被拒绝）
→ 再清超时/开关/变量/语句/临时表 → **最后健康检查**。
借出空闲连接时还要先 `ping`，剔除池内死亡连接。

## “任何一项复位失败都销毁”的判断依据

`pool.release()` 对每一步同时检查两件事：

1. **动作是否抛错**（网络错误、服务端报错）；
2. **动作之后状态是否真的等于期望值**（防“静默失败”——复位命令被吞、
   状态没变；模拟为 `fail_at[action]="noop"`）。

任一步不满足都抛 `ResetError` 并走销毁路径（`close()` + 计数），
绝不放回池，下次借出时建新连接。依据：

- 复位失败意味着协议流/服务端会话状态**未知**，无法确定哪些脏状态还在；
- 把状态未知的连接放回池，会把事务、超时、租户会话变量等泄漏给下一个
  使用者（已被 `reproduce_bug.py` 证明）；
- 重建一条连接的成本（一次握手）远低于向业务返回错误结果的成本。

## 回归测试覆盖

- **A 残留可观察**（8 例）：旧池行为下，8 类残留逐项能被下一个使用者观察到；
- **B 正常归还**（3 例）：完整复位、复用连接与新连接快照一致、干净连接正常复用；
- **C 归还时抛异常**（3 例）：业务异常经 `with pool.borrow()` 仍复位复用；
  复位动作 `raise` 时销毁、不放回、下次新建；`ResetError.step` 指出失败步骤；
- **D 部分复位失败**（2 例）：对 8 个步骤逐一注入静默 `noop`，校验捕获、
  销毁、不放回、下次新建，且错误精确定位到步骤名；
- **E 强制关闭**（3 例）：借出期间被 kill、空闲时被服务端断开（借出 ping 剔除）、
  死连接操作抛 `ConnectionClosed`；
- **F 清单完整性**（3 例）：清单覆盖全部快照键、顺序安全、
  弄脏全部状态后 `_reset` 能回到基准。

## 对拍数据（本机 Python 3.12 实测）

`crosscheck.py` 输出：

```
workload               snapshot==fresh  behavior==new  result
clean                  True             True           PASS
uncommitted_txn        True             True           PASS
aborted_txn            True             True           PASS
timeout_and_vars       True             True           PASS
unread_results         True             True           PASS
prepared_and_temp      True             True           PASS
everything             True             True           PASS
对拍结果: 7/7 通过
```

判定方法：对每种脏工作负载，先取**全新连接**跑同一探针（查询返回值、
fetchall 内容、BEGIN/INSERT/ROLLBACK 周期、复位前后快照）得到行为签名；
再取“弄脏→归还→再借出”的复用连接跑同一探针，两个签名逐字段相等才 PASS。
