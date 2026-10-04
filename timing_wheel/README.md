# timing_wheel — 休眠后集中触发问题的复现与修复

纯 Python 3 标准库实现，时间可注入（`now` 参数），测试用 `ManualClock` 模拟休眠。

## 问题

机器休眠期间时间轮不走字，唤醒后旧实现（`buggy.py`）在一次 `advance()`
里把积压的到期任务全部返回：休眠 1 小时、5000 个任务到期 → 同一时刻
5000 次触发，直接打爆下游。复现见 `tests/test_repro_burst.py`。

## 修复（`wheel.py` 的 `TimingWheel`）

不变式：**单次 `advance()` 最多触发 `max_fires_per_advance` 个任务，且只有
时钟实际前进时才消化积压**（调用方空转不会继续吐任务）。

### 过期任务策略：合并（coalesce）+ 限速消化

- 检测到时间跳变时，不逐 tick 重放，把所有过期任务按 (到期 tick, 提交顺序)
  合并进积压队列，随后每个 tick 按上限消化一批；
- 选「合并」而非「丢弃」：重试 / 心跳 / 超时关闭类任务通常仍需执行，丢弃
  会丢业务；「标记」则把补偿负担推给调用方。合并保留全部工作，只用速率
  保护下游；
- 对「睡太久补跑已无意义」的场景，提供可选 `late_policy="drop"` +
  `max_late_ms`：过期超阈值的任务被丢弃，并通过 `on_event("drop", ...)`
  显式标记。默认不启用。

### 触发分批数据（`python3 -m timing_wheel.demo_batches`）

5000 个任务在 1 小时休眠期间到期，上限 200/次：

```
 批次   时刻(s)  本批触发  剩余积压  合并跳tick
   1      0.0      200      4800      35999
   2      0.1      200      4600          0
   3      0.2      200      4400          0
  ...     ...      ...       ...        ...
  25      2.4      200         0          0
总批次 25，总触发 5000，单批最大 200（修复前：唤醒瞬间一次 5000）
```

drop 策略场景：休眠 1 小时、只补跑最近 10 秒 → 首批触发 100，
丢弃并标记 1000 个过期任务，剩余 50 个下一 tick 消化。

## 运行

```bash
# 回归测试（13 个用例：复现 2 + 修复 11）
python3 -m unittest discover -s timing_wheel/tests -t . -v

# 打印分批数据
python3 -m timing_wheel.demo_batches
```

## 测试覆盖

- `test_repro_burst.py`：对旧实现稳定复现集中触发（休眠 1 小时 / 5 秒）。
- `test_timing_wheel.py`
  - `NoJumpTest`：无跳变时按原定时刻触发、每 tick 不超上限；
  - `ShortJumpTest`：短时间跳变（几个 tick），受上限约束分批；
  - `LongSleepTest`：1 小时休眠，合并 + 限速消化，一个任务不丢、顺序不乱；
  - `LargeBacklogTest`：10000 个积压，批次数 = ceil(n/上限)，每批不超限；
  - `FrozenOrBackwardClockTest`：时钟不动不消化、回拨不炸；
  - `DropPolicyTest`：过期任务被丢弃并显式标记，未过期任务不受影响。

## 生产接入

```python
wheel = TimingWheel(tick_ms=100, max_fires_per_advance=200)  # now 默认 time.monotonic
while True:
    for payload in wheel.advance().fired:
        dispatch(payload)
    time.sleep(0.1)
```
