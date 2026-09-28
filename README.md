# 消息去重接收器（dedup-receiver）

上游会重复投递，本库保证同一消息只被处理一次，同时状态内存有界。
纯 Python 3 标准库实现，无第三方依赖。

## 文件

| 文件 | 说明 |
|---|---|
| `dedup_receiver.py` | 库源码（`DedupReceiver`），`__main__` 为统计输出样例 |
| `test_dedup_receiver.py` | 自测：边界、误判、乱序/跳跃/回绕、无法判定标识、统计自洽 |
| `memory_benchmark.py` | 内存基准：连续处理千万级消息，观测 RSS 与状态大小 |
| `throughput_benchmark.py` | 吞吐基准：窗口淘汰"全量扫描 vs 增量"在不同窗口大小下的对比 |

## 运行命令

```bash
python3 dedup_receiver.py                      # 统计输出样例
python3 -m unittest test_dedup_receiver -v     # 自测（16 个用例）
python3 memory_benchmark.py 10000000           # 内存基准（默认 1 千万条）
python3 throughput_benchmark.py                # 吞吐对比基准
```

## 设计

每条消息为 `{"id": str, "seq": int, "payload": ...}`：

- `id` 标识一条消息流；`seq` 为流内序号，序号空间 `2^seq_bits`，
  用模运算比较有向距离，**支持序号回绕**。
- 每个流维护滑动窗口：当前最大序号 `max_seq` + 最近 `window_size`
  条 `seq -> payload_hash` 记录。
- 判定规则（距离均为模 `2^seq_bits` 的有向距离）：

| 情形 | 行为 |
|---|---|
| `seq` 领先 `max_seq`（距离 < 2^(bits-1)） | 新消息，处理并前移窗口 |
| 领先距离 >= `window_size` | **大幅跳跃**：清空旧窗口，计 `jumps` |
| 落后距离 < `window_size`，哈希一致 | **窗口内重复**：丢弃，计 `duplicate` |
| 落后距离 < `window_size`，哈希不一致 | **同标识不同内容**：丢弃，计 `conflict` |
| 落后距离 < `window_size`，未见过该 seq | 乱序新消息，正常处理 |
| 落后距离 >= `window_size` | **窗口外迟到**：丢弃，计 `expired` |
| `id`/`seq` 缺失或非法 | **无法判定**：不交给正常处理器（避免重投被重复处理），计 `undetermined`，可用 `undetermined_handler` 兜底 |

- **内存有界**：流数量上限 `max_streams`（LRU 淘汰，计 `evictions`），
  每流窗口上限 `window_size`，状态总量 <= `max_streams * window_size`
  条小记录，与处理消息总量无关。
- **无法判定的消息与正常处理分开**：标识/序号缺失或非法的消息无法判断
  是否重复，若交给正常处理器，同一封消息重投一次就会被处理两次，违背
  "同一消息只处理一次"。因此这类消息只单独计数（`undetermined`），
  可选地交给构造时传入的 `undetermined_handler` 兜底，不进入 `handler`。
- **窗口前移增量淘汰**：每次前移只检查新滑出窗口的 delta 个序号
  （摊还 O(1)），不扫描整只窗口，吞吐不随 `window_size` 增大而下降。

## 误判风险量化

窗口外的迟到消息无法区分"迟到重复"与"被严重延迟的新消息"，本实现选择
**保守丢弃**（计 `expired`）。由此产生两类误判：

1. **重复被重投（漏判重复）**：重复副本到达时，其 `seq` 已滑出窗口
   （期间同流又处理了 >= `window_size` 条新消息），或其流状态被 LRU 淘汰。
   若重复投递延迟 D（以同流消息数计），则
   `P(漏判) = P(D >= window_size)`。
   例：D 服从均值 100 的指数分布时——
   - `window_size = 1024`：`P ≈ e^(-10.24) ≈ 3.6e-5`
   - `window_size = 2048`：`P ≈ e^(-20.48) ≈ 1.3e-9`
2. **新消息被误丢（误判过期）**： genuinely 新消息乱序延迟超过
   `window_size` 个位置时被当作过期丢弃。
   `P(误丢) = P(乱序位移 >= window_size)`；只要 `window_size` 大于
   系统最大乱序位移，误丢概率为 0。

即：**窗口越大，乱序容忍度越高，两类误判概率都指数下降**；代价是内存
线性增长（每流 `window_size` 条记录）。流数量超过 `max_streams` 时
LRU 淘汰会引入额外的漏判重复风险，应使 `max_streams` 覆盖活跃流数量。

对应测试（构造刚好越界的重复消息）：

- `test_boundary_inside_window`：落后距离 == `window_size - 1`，仍判重复；
- `test_boundary_just_outside_window`：落后距离 == `window_size`，判 `expired`；
- `test_late_duplicate_after_advance`：先处理 `seq=1`，再推进 `window_size`
  条新消息，`seq=1` 的迟到重复刚好越界判 `expired`，而落后 1 位的 `seq=2`
  仍是窗口内重复；
- `test_stream_lru_eviction`：流被淘汰后重投，演示漏判重复（按新流处理）。

## 内存数据（连续处理 1 千万条消息）

`python3 memory_benchmark.py 10000000`（512 流 × 窗口 256，实测）：

```
         已处理   RSS(MB)     流数      窗口记录数
   1,000,000      33.3    512    131,072
   5,000,000      33.3    512    131,072
  10,000,000      33.3    512    131,072

耗时 11.5s (871,118 msg/s)
最终状态: 512 个流, 131,072 条窗口记录 (上界 131,072)，与消息总量无关
```

RSS 全程持平在 33.3 MB，窗口记录数恒等于上界 `512 * 256 = 131,072`，
验证了状态大小不随消息总量增长。

## 吞吐对比（窗口淘汰：全量扫描 vs 增量淘汰）

`python3 throughput_benchmark.py`（每档 2 百万条，64 流基本有序，实测）：

```
    窗口         旧版(全量扫描)         新版(增量淘汰)      加速比
   256       164,486/s     1,580,058/s    9.61x
  1024        38,399/s     1,540,290/s   40.11x
  4096         7,843/s     1,475,775/s  188.16x
```

旧版每次窗口前移都全量扫描窗口字典（O(window_size)），窗口从 256 调到
4096 后吞吐从 16.4 万/s 跌到 0.78 万/s；新版按前移距离增量淘汰
（摊还 O(1)），吞吐稳定在 150 万/s 左右，不随窗口增大而下降。
两版判定结果完全一致（基准内置断言）。

## 统计输出样例

`python3 dedup_receiver.py`（节选）：

```
{'id': 'up-1', 'seq': 2, 'payload': 'b'}                   -> duplicate
{'id': 'up-1', 'seq': 2, 'payload': 'B!'}                  -> conflict
{'id': 'up-1', 'seq': 3, 'payload': 'c'}                   -> expired
{'id': 'up-1', 'seq': 0, 'payload': 'y'}                   -> processed   # 回绕
{'id': None, 'seq': 5, 'payload': 'no-id'}                 -> undetermined

统计输出:
  received   = 14
  processed  = 7
  duplicate  = 3
  expired    = 1
  conflict   = 1
  undetermined = 2
  jumps      = 3
  evictions  = 0
自洽校验通过: received == processed + duplicate + expired + conflict + undetermined
```

基准的统计自洽性：`9,880,102 + 99,846 + 9,489 + 0 + 10,563 = 10,000,000 = received`。
