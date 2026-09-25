"""内存基准：连续处理 10,000,000 条消息，验证状态大小与消息总量无关。

运行: python3 bench_memory.py
"""

import gc
import random
import resource
import tracemalloc

from dedup_receiver import DedupReceiver

TOTAL = 10_000_000
CHECKPOINTS = (1_000_000, 5_000_000, 10_000_000)
WINDOW_SIZE = 1024
MAX_SOURCES = 4096
NUM_SOURCES = 100_000  # 远多于 max_sources，强制 LRU 持续淘汰


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main() -> None:
    rng = random.Random(42)
    payloads = [f"payload-{i}".encode() for i in range(64)]
    rx = DedupReceiver(window_size=WINDOW_SIZE, max_sources=MAX_SOURCES)
    high = {}  # 各来源已发最大序号（基准脚本自身状态，大小取决于 NUM_SOURCES，与消息总量无关）

    gc.collect()
    tracemalloc.start()
    base_rss = rss_mb()
    print(f"{'messages':>12} {'rss_delta_MB':>13} {'py_peak_MB':>11} "
          f"{'sources':>8} {'evicted':>9}")

    next_checkpoint = iter(CHECKPOINTS)
    target = next(next_checkpoint)
    for i in range(1, TOTAL + 1):
        # 混合负载：80% 新消息（含偶发跳跃），10% 窗口内重复，
        # 5% 窗口内乱序，4% 窗口外迟到，1% 异常标识
        src = f"tenant-{rng.randrange(NUM_SOURCES)}"
        h = high.get(src, 0)
        roll = rng.random()
        if roll < 0.80:
            h += rng.choice((1, 1, 1, 1, 2, 7, 5000))  # 偶发大幅跳跃
            high[src] = h
            seq = h
        elif roll < 0.90:
            seq = max(0, h - rng.randrange(32))        # 近期序号：窗口内重复
        elif roll < 0.95:
            seq = max(0, h - rng.randrange(32, 900))   # 窗口内乱序
        elif roll < 0.99:
            seq = max(0, h - rng.randrange(2000, 100000))  # 窗口外迟到
        else:
            rx.receive(None, h)                        # 异常标识
            continue
        rx.receive(src, seq, payloads[seq & 63])       # 内容随序号确定，保证真重复内容一致

        if i == target:
            current, peak = tracemalloc.get_traced_memory()
            print(f"{i:>12,} {rss_mb() - base_rss:>13.2f} {peak / 2**20:>11.2f} "
                  f"{rx.tracked_sources:>8,} {rx.stats.sources_evicted:>9,}")
            target = next(next_checkpoint, -1)

    tracemalloc.stop()
    stats = rx.stats
    print("\n--- 最终统计 ---")
    for key, value in stats.as_dict().items():
        print(f"{key:>18}: {value:,}")
    print(f"\n统计自洽（received == 各类判定之和）: {stats.check_invariant()}")
    per_source = WINDOW_SIZE / 8 + WINDOW_SIZE * 2
    print(f"理论上界: {MAX_SOURCES} 来源 x (位图 {WINDOW_SIZE // 8}B + 指纹 "
          f"{WINDOW_SIZE * 2}B) ≈ {MAX_SOURCES * per_source / 2**20:.1f} MiB + 固定开销")


if __name__ == "__main__":
    main()
