# 对象池缺陷修复报告

语言：Python 3（仅标准库：`threading` / `collections` / `contextlib` / `time` / `unittest`）。

## 文件

| 文件 | 说明 |
| --- | --- |
| `buggy_pool.py` | 修复前的池实现，保留五类缺陷（用于复现） |
| `repro_defects.py` | 五类问题的稳定复现用例 |
| `object_pool.py` | 修复后的池实现 |
| `test_object_pool.py` | 回归测试（18 个用例，含并发独占与计数不变量断言） |

## 运行命令

```bash
python3 repro_defects.py                  # 复现修复前的五类问题
python3 -m unittest test_object_pool -v   # 修复后的回归测试
```

## 修复前后对照

| # | 问题 | 修复前（buggy_pool.py） | 修复后（object_pool.py） |
| --- | --- | --- | --- |
| 1 | 异常路径不归还，池枯竭 | 只有裸 `acquire`/`release`，异常即泄漏 | `pool.item()` 上下文管理器 + `try/finally` 双保险，异常也归还 |
| 2 | 同一对象归还两次后被两人持有 | `release` 不校验，直接入列 | 在途集合 `_in_use` 校验，重复/外来归还抛 `ReleaseError` |
| 3 | 超时后悄悄越界建对象 | 超时分支无条件 `factory()` | 只有 `total < max_size` 才新建；超时抛 `PoolTimeout` |
| 4 | 超时获取返回已销毁对象 | `close()` 销毁后对象仍留在空闲列表 | `close()` 销毁并清空空闲列表；获取前必查 `closed` 标志 |
| 5 | 计数与实际不一致 | `_created` 只增不减、`_in_use` 可被扣成负数 | 单一事实源：`total == len(idle) + len(in_use)`，销毁即扣减，`check_invariants()` 断言 |

## 语义约定（修复后）

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
回归测试：`test_context_manager_releases_on_exception`、
`test_try_finally_releases_on_exception`。

## 关键设计

- **互斥与独占**：所有状态变更都在同一把 `threading.Condition` 锁下完成；
  对象从 `_idle` 弹出即加入 `_in_use` 集合，归还时必须先存在于 `_in_use`，
  因此同一对象不可能同时处于两个调用方手中（并发测试
  `test_exclusivity_and_counter_invariants` 用 8 线程 × 300 轮验证）。
- **计数守恒**：`total` 只在新建（+1）与销毁（-1）时变化，且与
  `len(_idle) + len(_in_use)` 严格相等；工厂抛异常时回滚预留的计数。
- **关闭安全**：`close()` 持锁置标志、销毁空闲对象、唤醒所有等待者；
  等待循环每次醒来先检查 `closed`，杜绝拿到已销毁对象的竞态
  （`test_no_destroyed_object_after_close_race` 反复触发归还/关闭竞态）。
- **取舍**：`factory` 在锁内调用以保证“任意时刻计数与实际一致”的强不变量；
  代价是慢工厂会串行化新建（不影响已有对象的并发借还）。若工厂很慢，
  可改为“锁外创建 + 槽位预留”，但需放宽不变量为 `total <= idle + in_use + reserved`。
