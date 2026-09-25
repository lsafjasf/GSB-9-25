"""统计输出样例：模拟突发流量，打印两种限流器的可观测统计。

运行：python3 demo.py
"""

import threading
import time

from ratelimit import SlidingWindowRateLimiter, TokenBucketRateLimiter


def show(name, limiter):
    s = limiter.stats()
    print(
        f"{name:<14} 放行={s.allowed:<5} 拒绝={s.rejected:<5} "
        f"水位={s.level:<8.2f} 平均等待={s.avg_wait_seconds * 1000:8.3f}ms "
        f"时钟回拨={s.clock_backwards}"
    )


def main():
    bucket = TokenBucketRateLimiter(capacity=20, rate_per_second=50)
    window = SlidingWindowRateLimiter(max_requests=20, window_seconds=1.0)

    def burst(limiter, count):
        def worker():
            for _ in range(count // 8):
                limiter.try_acquire()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    print("== 瞬时突发 200 个请求（8 线程并发）==")
    burst(bucket, 200)
    burst(window, 200)
    show("TokenBucket", bucket)
    show("SlidingWindow", window)

    print("\n== 0.5 秒后再突发 200 个 ==")
    time.sleep(0.5)
    burst(bucket, 200)
    burst(window, 200)
    show("TokenBucket", bucket)   # 桶 0.5s 内已补满，又可突发 20
    show("SlidingWindow", window) # 第一批记录仍在 1s 窗口内，几乎不放行


if __name__ == "__main__":
    main()
