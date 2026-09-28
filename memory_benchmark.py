"""内存有界性基准：连续处理千万级消息，观测 RSS 与接收器状态大小。

运行：python3 memory_benchmark.py [消息总数，默认 10000000]
"""

import random
import resource
import sys
import time

from dedup_receiver import DedupReceiver

N_STREAMS = 512          # 并发消息流数量（<= max_streams，无淘汰）
WINDOW = 256             # 每流窗口大小
DUP_RATE = 0.01          # 1% 窗口内重复（重发最近消息）
EXPIRED_RATE = 0.001     # 0.1% 窗口外迟到重复
BAD_ID_RATE = 0.001      # 0.1% 标识缺失


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main():
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000_000
    rng = random.Random(20260925)
    rx = DedupReceiver(window_size=WINDOW, max_streams=N_STREAMS)
    counters = [0] * N_STREAMS
    recent = [None] * 4096          # 有界环形缓冲，用于注入窗口内重复
    msg = {"id": None, "seq": 0, "payload": ""}  # 复用同一个 dict，避免测量噪音

    print(f"处理 {total:,} 条消息 | 流数={N_STREAMS} 窗口={WINDOW}")
    print(f"{'已处理':>12s} {'RSS(MB)':>9s} {'流数':>6s} {'窗口记录数':>10s}")
    start = time.time()
    for i in range(total):
        roll = rng.random()
        sid = i % N_STREAMS
        if roll < DUP_RATE and recent[i % 4096] is not None:
            old_sid, old_seq = recent[i % 4096]
            msg["id"], msg["seq"] = f"up-{old_sid}", old_seq
        elif roll < DUP_RATE + EXPIRED_RATE and counters[sid] > 1000:
            msg["id"], msg["seq"] = f"up-{sid}", counters[sid] - 1000
        elif roll < DUP_RATE + EXPIRED_RATE + BAD_ID_RATE:
            msg["id"], msg["seq"] = None, counters[sid]
        else:
            counters[sid] += 1
            msg["id"], msg["seq"] = f"up-{sid}", counters[sid]
            recent[i % 4096] = (sid, counters[sid])
        msg["payload"] = f"payload-{msg['seq']}"
        rx.receive(msg)
        if (i + 1) % 1_000_000 == 0:
            streams, entries = rx.state_size()
            print(f"{i + 1:>12,} {rss_mb():>9.1f} {streams:>6d} {entries:>10,d}")

    elapsed = time.time() - start
    print(f"\n耗时 {elapsed:.1f}s ({total / elapsed:,.0f} msg/s)")
    print("统计输出:")
    for key, val in rx.stats.items():
        print(f"  {key:10s} = {val:,}")
    rx.check_consistency()
    streams, entries = rx.state_size()
    print(f"\n最终状态: {streams} 个流, {entries:,} 条窗口记录 "
          f"(上界 {N_STREAMS * WINDOW:,})，与消息总量 {total:,} 无关")
    print("自洽校验通过: received == processed + duplicate + expired + conflict + undetermined")


if __name__ == "__main__":
    main()
