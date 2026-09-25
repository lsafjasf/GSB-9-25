"""性能测试：十万条记录的构建与完整校验耗时、增量校验耗时。

运行：python3 bench.py [N]   （默认 N=100000）
"""

import sys
import time

from audit_chain import AuditChain


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000

    c = AuditChain()
    t0 = time.perf_counter()
    for i in range(n):
        c.append({"op": "write", "row": i, "user": f"u{i % 11}",
                  "detail": "x" * 32})
    t1 = time.perf_counter()

    anchor = c.anchor()

    # 完整校验（无锚点）
    c.verify()  # 预热
    t2 = time.perf_counter()
    r = c.verify()
    t3 = time.perf_counter()
    assert r.ok and r.checked == n

    # 完整校验（带锚点）
    t4 = time.perf_counter()
    r2 = c.verify(anchor=anchor)
    t5 = time.perf_counter()
    assert r2.ok

    # 增量校验：任意位置 1000 条区间
    t6 = time.perf_counter()
    r3 = c.verify(start=n // 2, stop=n // 2 + 1000)
    t7 = time.perf_counter()
    assert r3.ok and r3.checked == 1000

    print(f"记录数 N                : {n:,}")
    print(f"构建（含哈希与自检）    : {t1 - t0:.3f} s  ({n / (t1 - t0):,.0f} 条/s)")
    print(f"完整校验（无锚点）      : {t3 - t2:.3f} s  ({n / (t3 - t2):,.0f} 条/s)")
    print(f"完整校验（带锚点比对）  : {t5 - t4:.3f} s")
    print(f"增量校验（任意 1000 条）: {(t7 - t6) * 1000:.2f} ms")


if __name__ == "__main__":
    main()
