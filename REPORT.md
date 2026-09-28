# 对象池缺陷修复报告

语言：Python 3（仅标准库：`threading` / `collections` / `contextlib` / `time` / `unittest`）。

## 文件

| 文件 | 说明 |
| --- | --- |
| `buggy_pool.py` | 修复前的池实现，保留五类缺陷（用于复现） |
| `repro_defects.py` | 五类问题的稳定复现用例 |
| `object_pool.py` | 修复后的池实现（按借出凭据 Lease 校验归还） |
| `test_object_pool.py` | 回归测试（22 个用例，含并发独占、计数不变量与陈旧二次归还断言） |

## 运行命令

```bash
python3 repro_defects.py                  # 复现修复前的五类问题
python3 -m unittest test_object_pool -v   # 修复后的回归测试
```

## 修复前后对照

| # | 问题 | 修复前（buggy_pool.py） | 修复后（object_pool.py） |
| --- | --- | --- | --- |
| 1 | 异常路径不归还，池枯竭 | 只有裸 `acquire`/`release`，异常即泄漏 | `pool.item()` 上下文管理器 + `try/finally` 双保险，异常也归还 |
| 2 | 同一对象归还两次后被两人持有 | `release` 不校验，直接入列 | `release` 只接受 `acquire` 签发的借出凭据 `Lease`；重复/陈旧/外来归还抛 `ReleaseError` |
| 3 | 超时后悄悄越界建对象 | 超时分支无条件 `factory()` | 只有 `total < max_size` 才新建；超时抛 `PoolTimeout` |
| 4 | 超时获取返回已销毁对象 | `close()` 销毁后对象仍留在空闲列表 | `close()` 销毁并清空空闲列表；获取前必查 `closed` 标志 |
| 5 | 计数与实际不一致 | `_created` 只增不减、`_in_use` 可被扣成负数 | 单一事实源：`total == len(idle) + len(in_use)`，销毁即扣减，`check_invariants()` 断言 |

## 语义约定（修复后）

- **借出凭据**：`acquire()` 返回 `Lease`（用 `lease.obj` 取对象），`release(lease)`
  只接受当前在途的凭据。凭据按次签发、归还即失效；同一对象被再次借出时
  签发的是新凭据，因此旧持有者的“陈旧二次归还”无法把他人正在使用的对象
  还回池里（若只用集合成员关系校验，此时成员检查照样通过，对象会同时进入
  空闲集合与在用集合，被第三方再次取走）。
- **获取超时**：`acquire(timeout)` 超时抛 `PoolTimeout`（`TimeoutError` 子类），
  不返回 `None`、不返回已销毁对象；`timeout=None` 无限等待。
- **池关闭**：`close()` 销毁全部空闲对象并 `notify_all` 唤醒等待者（抛 `PoolClosed`）；
  在途对象归还时被销毁而不再入池；`close()` 幂等。
- **关闭后获取**：立即抛 `PoolClosed`。
- **计数不变量**：任意时刻 `total == idle + in_use`、`0 <= total <= max_size`、
  `total <= peak <= max_size`、空闲集与在途集互斥；`stats()` 在锁内生成快照，
  `check_invariants()` 可随时断言。

## 异常路径如何保证归还

1. 推荐用法——上下文管理器（内部即 `try/finally`）：

   ```python
   with pool.item(timeout=1.0) as conn:
       conn.query(...)      # 抛异常也会走 finally 归还
   ```

2. 手写等价结构：

   ```python
   conn = pool.acquire(timeout=1.0)
   try:
       conn.query(...)
   finally:
       pool.release(conn)
   ```

`ObjectPool.item()` 用 `contextlib.contextmanager` 实现，`yield` 包在
`try/finally` 中，任何异常（包括 `BaseException`）都会触发 `release`。
手写 `try/finally` 时注意：`acquire` 返回的是凭据，应 `pool.release(lease)`。
回归测试：`test_context_manager_releases_on_exception`、
`test_try_finally_releases_on_exception`。

## 关键设计

- **凭据即能力（capability）**：所有状态变更都在同一把 `threading.Condition`
  锁下完成；`_in_use` 保存的是每次借出独立签发的 `Lease` 而非对象本身，
  归还按凭据身份校验。对象被再次借出时旧凭据已失效，陈旧归还必抛
  `ReleaseError`，因此同一对象不可能同时处于两个调用方手中（并发测试
  `test_exclusivity_and_counter_invariants` 用 8 线程 × 300 轮验证了
  一般独占性；`test_concurrent_stale_release_keeps_exclusivity` 与
  `test_stale_release_storm` 专门构造并发陈旧二次归还）。
- **计数守恒**：`total` 只在新建（+1）与销毁（-1）时变化，且与
  `len(_idle) + len(_in_use)` 严格相等；工厂抛异常时回滚预留的计数。
- **关闭安全**：`close()` 持锁置标志、销毁空闲对象、唤醒所有等待者；
  等待循环每次醒来先检查 `closed`，杜绝拿到已销毁对象的竞态
  （`test_no_destroyed_object_after_close_race` 反复触发归还/关闭竞态）。
- **取舍**：`factory` 在锁内调用以保证“任意时刻计数与实际一致”的强不变量；
  代价是慢工厂会串行化新建（不影响已有对象的并发借还）。若工厂很慢，
  可改为“锁外创建 + 槽位预留”，但需放宽不变量为 `total <= idle + in_use + reserved`。
