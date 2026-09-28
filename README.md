# 请求响应关联层（rrc）

Python 3，仅标准库（asyncio）。多个并发调用共用一个通道，响应按唯一 id 配对；
超时/取消后无任何悬挂状态。底层通道为内置模拟器，可注入延迟、乱序、丢包、重放与断连。

## 结构
- `rrc/channel.py` — `SimulatedChannel`：随机延迟（乱序）、`loss` 丢包、`replay` 重放、
  `slow` 长尾延迟、`disconnect()/reconnect()`；通道侧计数（sent/delivered/dropped/
  replayed/slow）用于对账。
- `rrc/client.py` — `Client`：关联表 + 有界墓碑表（容量 + TTL）+ 并发上限 + 分类统计。
- `tests/test_correlation.py` — 15 个单元测试。
- `stress.py` — 10 万次请求压力测试（乱序 + 丢包 + 重放 + 长尾），含内存两份数字。

## 语义
- 响应配对：未配对的响应按四类分别计数并丢弃，不触碰任何状态——
  - `duplicate`：重复响应（重放副本，墓碑终态为 ok）
  - `late`：迟到响应（请求已超时/取消/失败），并按子原因细分
    `late_timeout` / `late_cancelled` / `late_failed`，三者之和恒等于 `late`
  - `expired`：过期响应（id 在已发范围内，但墓碑已被 TTL 或容量驱逐）
  - `unknown`：未知标识（id 超出已发序号范围或非法）
- 墓碑表：`tombstone_size`（容量）与 `tombstone_ttl`（存活秒数）均可配置，
  双重约束、懒惰驱逐；`tombstone_ttl=None` 表示仅受容量约束。
- 超时隔离：超时即完结并移除关联项；迟到响应只计 `late`，不改写任何状态。
- 并发上限：`on_full="fail"` 快速失败（`BusyError`）或 `"queue"` 排队。
- 取消：`cancel(rid)` 或取消协程，关联表项与计时器立即回收。
- 断连：`on_disconnect="fail"` 立即失败全部在途请求；`"retry"` 缓冲、重连后重发，
  仍受原始超时约束——任何情况下请求都有确定结果，绝不悬挂。
- 统计自洽不变式：`succeeded + timed_out + failed + cancelled == submitted`，
  由 `Client.check_consistent()` 断言。
- 分类对账不变式：进入客户端的每条响应被且只被分类一次——
  `delivered + injected == matched + duplicate + late + expired + unknown`，
  由压力测试逐场景断言。

## 内存上界推导
墓碑表是唯一随时间累积的状态，其占用有界且可复算：

- 条目数上界：`N <= min(tombstone_size, ceil(吞吐 x tombstone_ttl))`，与请求总量无关。
- 字节数上界：构造同构探针表实测深层字节数再乘 2（CPython 字典删除不收缩，
  稳态 entries 数组至多含等量 dummy 槽位），见 `Client.tombstone_bound()`。
- 单条成本实测（CPython 3.12 / 64-bit）：约 193 B
  （OrderedDict 节点 + int 键 + (终态, 过期时刻) 元组；终态字符串为共享常量不计），
  见 `Client.tombstone_entry_cost()`。
- 实测占用由 `Client.tombstone_memory()` 深层计量（共享对象只计一次）。

压力场景输出两份内存数字：**墓碑表占用**（实测 vs 推导上界）与**清表后残留**
（清空墓碑表并 gc 后相对基线的堆增长，须 < 10 MB）。

## 运行
```bash
python3 -m unittest discover -s tests -v   # 单元测试（乱序/超时隔离/取消/上限/断连/墓碑分类）
python3 stress.py                          # 10 万请求压力 + 分类对账 + 内存两份数字
python3 stress.py 200000                   # 自定义请求数
python3 stress.py 100000 1024 0.5          # 自定义墓碑容量与 TTL（秒）
```

默认配置下场景 A 实测（CPython 3.12，10 万请求）：
- duplicate = 476，与通道重放计数 476 精确一致；late = 516（全部 late_timeout）；
  expired = 0；unknown = 3（注入幽灵数）。
- 墓碑表占用实测 14.94 MB <= 推导上界 23.07 MB（65536 条 x 193 B x 2）；
  清表后残留 +0.09 MB。
- 场景 B（容量 256 + TTL 0.3s）：迟到的长尾响应全部转为 expired，占用 0.027 MB。
