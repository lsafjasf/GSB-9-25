# 状态与取消语义说明

## 状态机

```
PENDING(未开始) ──> RUNNING(执行中) ──> SUCCESS(成功)
                 │                 └──> FAILED(失败)
                 └──> CANCELLED(已取消) <──┘（任意非终态均可被取消）
```

- 五种状态：`PENDING` / `RUNNING` / `SUCCESS` / `FAILED` / `CANCELLED`（`TaskState` 枚举）。
- 终态：`SUCCESS`、`FAILED`、`CANCELLED`。进入终态后状态不可再变。
- 所有状态迁移共用同一把条件锁（`TaskResult._cond`），迁移是原子的。

## 取消语义：先到先得（first-wins）

- `cancel()` 与任务的「完成/失败」竞争时，**谁先拿到锁提交终态迁移，谁决定最终状态**；
  结果确定，不存在中间态或双写。
- 取消生效后（`cancel()` 返回 `True`）：
  - 不再写结果（worker 的完成迁移必然失败）；
  - 不再触发回调（适配层只在成功/失败迁移生效时触发回调）；
  - 协作式取消标志置位，任务内可通过 `token.cancelled` / `token.throw_if_cancelled()` 感知。
- 取消未生效（任务已进入终态，`cancel()` 返回 `False`）：状态与结果保持原样。
- `cancel()` 幂等：重复取消安全，第一次之后均返回 `False`。
- 任务内自取消：`token.cancel()` 等价于外部 `handle.cancel()`；任务随后即使正常返回，
  结果也不会被写入，终态为 `CANCELLED`。
- 任务主动抛 `CancelledError` 视为协作式取消（终态 `CANCELLED` 而非 `FAILED`）。

## 资源释放

- 每个任务独占一个 `TaskResource`（文件句柄 + 内存缓冲）。
- 无论成功、失败、取消，资源都在 worker 的 `finally` 中关闭（幂等），
  且**先于终态迁移提交**：`wait()`/`result()` 返回时，成功/失败路径的资源必已释放。
- `handle.join(timeout)` 等待工作线程完全退出，用于在取消竞争场景下确认资源已释放。
- 测试通过 `ResourceTracker.live_count == 0` 断言无泄漏。

## 异常透传

- 任务抛出的异常对象原样存入 `TaskResult.error`，`result()` 重新抛出同一对象：
  类型与消息不包装、不丢失。
- 适配层 `submit_with_callback(fn, callback)` 与旧接口 `legacy_task.submit_task` 签名一致，
  `callback(error, result)` 收到的错误对象与旧实现逐例比对一致（见 `EquivalenceTests`）。

## 运行命令

```bash
cd /home/administrator/gsb/uid29/B
python3 -m unittest test_regression -v
```
