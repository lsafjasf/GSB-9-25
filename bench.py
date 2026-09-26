# -*- coding: utf-8 -*-
"""基准：渲染 100,000 次并报告耗时。"""
import time

from msgfmt import Formatter


def main():
    fmt = Formatter.from_files("locales.json", "messages.json")
    n = 100_000

    # 预热（模板编译缓存）
    fmt.format("en", "inbox.summary", user="Ana", count=1, date=(2026, 9, 27))

    counts = [0, 1, 1.5, 2, 5, 1234567.5]
    start = time.perf_counter()
    for i in range(n):
        fmt.format("en", "inbox.summary", user="Ana",
                   count=counts[i % len(counts)], date=(2026, 9, 27))
    elapsed = time.perf_counter() - start

    print("renders      : %d" % n)
    print("total time   : %.3f s" % elapsed)
    print("per render   : %.2f us" % (elapsed / n * 1e6))
    print("throughput   : %.0f renders/s" % (n / elapsed))

    sample = fmt.format("en", "inbox.summary", user="Ana", count=1.5, date=(2026, 9, 27))
    print("sample       : %s" % sample.text)


if __name__ == "__main__":
    main()
