# TimingWheel：休眠后定时任务集中触发的修复

## 问题

进程所在机器休眠后，时钟向前跳变，时间轮把休眠期间积压的到期任务在
一次 tick 里全部触发，瞬间打爆下游。

## 复现

`test_timing_wheel.py` 中的 `BurstReproTest` 用可注入的 `FakeClock`
模拟休眠 1 小时：5000 个任务在**一次** `poll` 中全部返回
（`max_fires_per_poll=None` 对应修复前的旧行为）。

## 修复方案

见 `timing_wheel.py`：

1. **时间可注入**：构造参数 `clock` 是唯一时间来源，测试可自由跳变。
2. **单批上限**：到期任务先进入 ready 队列，`poll` 每次最多返回
   `max_fires_per_poll` 个（默认 1000），连续调用 `poll` 按批排空，
   同一时刻的触发数量有硬上限。
3. **过期策略：标记（mark）**。每个触发结果带 `overdue_ms`，
   不丢弃、不隐式合并。理由：定时任务通常不保证幂等，丢弃会直接
   丢业务；合并需要 key 语义，改变调用方行为；标记把决策权交给
   调用方（可按 `overdue_ms` 自行丢弃过老任务，或按 `key` 去重），
   是对下游最安全的默认策略。

## 触发分批数据

`python3 demo_batches.py` 的输出（已存于 `batch_data.txt`）：

- 修复前：1 批 × 5000，单批最大 5000（打爆下游）。
- 修复后（上限 200/批）：25 批 × 200，单批最大 200，
  全部任务带 `overdue_ms` 标记，按截止时间顺序出队，不丢不重。

## 回归测试

覆盖：复现集中触发、长时间休眠（1h/2h）、短时间跳变（350ms）、
大量积压（5 万任务）、无跳变正常走时（不提前、不多轮误触发）、
取消任务不触发。

```bash
cd timing_wheel
python3 -m unittest test_timing_wheel -v   # 7 个用例
python3 demo_batches.py                    # 查看分批数据
```

仅依赖 Python 3 标准库。
