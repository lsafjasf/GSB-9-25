"""统计输出样例：20 万条混合负载，展示各类计数与自洽校验。

运行: python3 demo_stats.py
"""

import random

from dedup_receiver import DedupReceiver

TOTAL = 200_000


def main() -> None:
    rng = random.Random(7)
    delivered = []
    rx = DedupReceiver(window_size=256, max_sources=512,
                       on_message=lambda s, q, p: delivered.append((s, q)))
    high = {}
    for _ in range(TOTAL):
        src = f"tenant-{rng.randrange(500)}"
        h = high.get(src, 0)
        roll = rng.random()
        if roll < 0.80:                       # 新消息（偶发跳跃）
            h += rng.choice((1, 1, 1, 2, 9, 3000))
            high[src] = h
            rx.receive(src, h, f"payload-{h & 63}".encode())
        elif roll < 0.90:                     # 窗口内重复（内容一致）
            seq = max(0, h - rng.randrange(16))
            rx.receive(src, seq, f"payload-{seq & 63}".encode())
        elif roll < 0.94:                     # 窗口内乱序
            rx.receive(src, max(0, h - rng.randrange(16, 200)))
        elif roll < 0.99:                     # 窗口外迟到
            rx.receive(src, max(0, h - rng.randrange(1000, 50000)))
        elif roll < 0.995:                    # 异常标识
            rx.receive(None, h)
        else:                                 # 标识重复但内容不同
            seq = max(0, h - rng.randrange(16))
            rx.receive(src, seq, b"tampered-content")

    stats = rx.stats
    print("--- 统计输出样例 ---")
    for key, value in stats.as_dict().items():
        print(f"{key:>18}: {value:,}")
    print(f"\nexactly-once 交付次数   : {len(delivered):,}（应等于 accepted）")
    print(f"统计自洽                : {stats.check_invariant()}")


if __name__ == "__main__":
    main()
