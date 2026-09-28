"""吞吐基准：窗口前移从"全量扫描"改为"增量淘汰"前后的吞吐对比。

旧版行为由 LegacyDedupReceiver 忠实复现（与修复前 _prune 的逐条全量扫描
完全一致，仅搬入 _evict 钩子）。场景为各流基本有序到达（每条消息都推进
窗口，是剪枝开销的最大化场景）。

运行：python3 throughput_benchmark.py [每档消息数，默认 2000000]
"""

import sys
import time

from dedup_receiver import DedupReceiver

N_STREAMS = 64
WINDOWS = (256, 1024, 4096)


class LegacyDedupReceiver(DedupReceiver):
    """修复前行为：每次窗口前移都全量扫描窗口字典，O(window_size)。"""

    def _evict(self, state, max_seq, delta):
        limit = self.window_size
        mask = self._mask
        window = state.window
        for old in [k for k in window if ((max_seq - k) & mask) >= limit]:
            del window[old]


def bench(cls, window, total):
    rx = cls(window_size=window, max_streams=N_STREAMS)
    counters = [0] * N_STREAMS
    msg = {"id": None, "seq": 0, "payload": "p"}  # 复用 dict，避免测量噪音
    start = time.perf_counter()
    for i in range(total):
        sid = i % N_STREAMS
        counters[sid] += 1
        msg["id"], msg["seq"] = f"up-{sid}", counters[sid]
        rx.receive(msg)
    elapsed = time.perf_counter() - start
    return total / elapsed, rx


def main():
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000
    print(f"每档 {total:,} 条消息 | {N_STREAMS} 条流，基本有序（逐条推进窗口）")
    print(f"{'窗口':>6s} {'旧版(全量扫描)':>16s} {'新版(增量淘汰)':>16s} {'加速比':>8s}")
    for window in WINDOWS:
        old_rate, old_rx = bench(LegacyDedupReceiver, window, total)
        new_rate, new_rx = bench(DedupReceiver, window, total)
        # 两版判定结果必须一致，仅性能不同
        assert old_rx.stats == new_rx.stats, (old_rx.stats, new_rx.stats)
        print(f"{window:>6d} {old_rate:>13,.0f}/s {new_rate:>13,.0f}/s"
              f" {new_rate / old_rate:>7.2f}x")
    print("\n两版统计输出一致（已断言），仅窗口淘汰策略不同。")


if __name__ == "__main__":
    main()
