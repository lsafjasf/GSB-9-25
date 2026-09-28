"""窗口推进吞吐基准：对比不同 window_size 下的处理速度。

运行：python3 throughput_benchmark.py [消息数，默认 200000]
"""

import sys
import time

from dedup_receiver import DedupReceiver

WINDOWS = (256, 512, 1024, 2048, 4096)


def bench(total, window_size, n_streams=64):
    rx = DedupReceiver(window_size=window_size, max_streams=n_streams)
    counters = [0] * n_streams
    msg = {"id": "", "seq": 0, "payload": ""}
    start = time.perf_counter()
    for i in range(total):
        sid = i % n_streams
        counters[sid] += 1
        msg["id"] = f"up-{sid}"
        msg["seq"] = counters[sid]
        msg["payload"] = f"payload-{counters[sid]}"
        rx.receive(msg)
    elapsed = time.perf_counter() - start
    return total / elapsed


def main():
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    print(f"顺序消息 {total:,} 条 | 64 流 | 每窗口档位独立计时")
    print(f"{'window_size':>12s} {'吞吐(msg/s)':>16s}")
    for w in WINDOWS:
        rate = bench(total, w)
        print(f"{w:>12d} {rate:>16,.0f}")


if __name__ == "__main__":
    main()
