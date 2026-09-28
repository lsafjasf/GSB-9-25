# streampipe — 有界缓冲 + 背压的流式数据处理管道

纯 Python 3 标准库实现（`threading` / `collections.deque`），无第三方依赖。

## 文件

- `streampipe.py` — 库源码（末尾附 `__main__` 演示）
- `test_streampipe.py` — 自测（unittest，15 个用例）

## 运行命令

```bash
python3 test_streampipe.py        # 运行全部测试（-v 看明细）
python3 streampipe.py             # 运行演示，打印统计输出样例
```

## 设计

### 有界缓冲与背压

每两个相邻阶段之间、生产者到首阶段、末阶段到消费者之间各有一个容量为
`capacity` 的 `BoundedBuffer`。缓冲满时按 `policy` 处理：

| 策略 | 行为 |
|---|---|
| `block`（默认） | `put` 阻塞，支持 `timeout`（抛 `BackpressureTimeout`）和 `cancel_event`（抛 `PutCancelled`） |
| `drop_oldest` | 丢弃最旧元素，写入永不阻塞 |
| `drop_newest` | 丢弃新元素，`put` 返回 `False` |

### 内存上界推导

设阶段数 `S`，每缓冲容量 `C`，缓冲数 `B = S + 1`：

- 每个 `BoundedBuffer` 在同一条件变量下保证 `len(items) <= C`（丢弃策略同样不超过 `C`）；
- 每个阶段工作线程在缓冲之外至多持有 1 个正在处理的元素；
- 因此管道内部驻留元素总数 `<= B*C + S = (S+1)*C + S`。

若单个元素大小 `<= m` 字节，则管道峰值内存 `<= ((S+1)*C + S) * m + O(固定开销)`，
与输入总量无关。测试用全局在途计数器（`Stats.peak_buffered`）和每缓冲高水位
（`Stats.per_buffer_peak`）验证该上界：连续写入 5000 个元素、容量仅 4 时，
`peak_buffered <= (3+1)*4 = 16` 且每个缓冲峰值 `<= 4`。

### 错误传播

任一阶段抛异常 → 记录错误 → 关闭全部缓冲 → **丢弃**所有已缓冲数据（计入
`dropped`，不静默吞掉）→ 消费者迭代器**原样抛出该异常**（类型与消息不变，
调用方可直接 `except ValueError` 等具体类型，无需再翻 `__cause__`）
→ 生产者后续 `put` 抛 `ClosedError`。已交付给消费者的结果保持有效。
仅 `abort()` 中止路径抛 `PipelineError`。

### 关闭语义

- `close()`：优雅关闭，幂等。之后 `put` 抛 `ClosedError`；已缓冲数据被处理完，
  消费者能读到剩余全部数据。
- `abort()`：立即终止（消费者异常时由 `with` 语句自动调用），幂等。丢弃全部
  缓冲数据（计入 `dropped`），`put` 抛 `ClosedError`，消费者迭代抛 `PipelineError`。
- 放弃迭代：消费者直接关闭 `results()` 生成器（如 `break` 后对象被回收，
  未走上下文管理也未调用 `close/abort`）等价于 `abort()`——阻塞在满缓冲上的
  生产者和所有阶段线程会被立即解除并退出，不会永久卡死。

## 覆盖的场景（对应测试）

- 消费者很慢：`test_memory_bound_under_fast_producer_slow_consumer`、`test_put_blocks_until_consumer_drains`
- 消费者异常：`test_consumer_exception_aborts_via_context_manager`
- 上游提前关闭：`test_graceful_close_drains_remaining`
- 生产者在背压期间被取消：`test_put_cancelled_during_backpressure`、`test_put_timeout_under_backpressure`
- 阶段抛错传播（原始类型直接可见）：`test_stage_error_terminates_pipeline_and_propagates`、`test_first_stage_error_still_propagates`
- 中途放弃迭代：`test_abandoned_iteration_lets_all_threads_exit`
- 内存上界：`TestMemoryBound`（快生产者 + 慢消费者 + 丢弃策略）
- 关闭语义：`TestCloseSemantics`（幂等关闭、关闭后写入失败、剩余数据可读）

## 统计输出样例

`python3 streampipe.py`（3 阶段、容量 8、写入 1000 个元素，其中 1/5 被过滤）：

```
consumed 800/1000 items
Stats(written=1000, processed=2600, dropped=200, failed=0, peak_buffered=32 (bound=32), per_buffer_peak=(8, 8, 8, 8), aborted=False)
```

字段含义：`written` 生产者成功写入数；`processed` 各阶段处理并交付下游的累计数；
`dropped` 丢弃总数（策略丢弃 + 失败/中止丢弃 + `SKIP` 过滤）；`failed` 阶段失败次数；
`peak_buffered` 全局驻留缓冲峰值（不超过 `bound = (S+1)*C`）；`per_buffer_peak`
每个缓冲的高水位（各自不超过 `capacity`）。
