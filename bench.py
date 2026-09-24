"""性能基准：10 万条记录的加解密与轮换耗时。"""

import os
import platform
import statistics
import sys
import time

import field_crypto as fc

N = 100_000


def bench(n=N):
    store = fc.KeyStore()
    store.add(1, fc.generate_key())

    # 模拟真实敏感字段：~120 字节
    plaintexts = [os.urandom(96) + b"id-number-11010119900101" for _ in range(n)]
    aads = [f"record:{i}".encode() for i in range(n)]
    payload = sum(len(p) for p in plaintexts)

    t0 = time.perf_counter()
    tokens = [fc.encrypt(store, p, aad=a) for p, a in zip(plaintexts, aads)]
    t_enc = time.perf_counter() - t0

    t0 = time.perf_counter()
    recovered = [fc.decrypt(store, t, aad=a) for t, a in zip(tokens, aads)]
    t_dec = time.perf_counter() - t0
    assert recovered == plaintexts

    # 轮换：新增 v2，重加密全部记录
    store.add(2, fc.generate_key())
    store.set_active(2)
    t0 = time.perf_counter()
    rotated = [fc.rotate(store, t, aad=a) for t, a in zip(tokens, aads)]
    t_rot = time.perf_counter() - t0
    assert all(fc.peek_key_version(t) == 2 for t in rotated)

    print(f"platform : {platform.python_implementation()} {platform.python_version().split()[0]} "
          f"on {platform.machine()}")
    print(f"records  : {n:,}  (avg field {payload / n:.0f} B, total {payload / 1e6:.1f} MB)")
    print(f"encrypt  : {t_enc:6.2f}s  {n / t_enc:9,.0f} ops/s  "
          f"{t_enc / n * 1e6:6.1f} us/op")
    print(f"decrypt  : {t_dec:6.2f}s  {n / t_dec:9,.0f} ops/s  "
          f"{t_dec / n * 1e6:6.1f} us/op")
    print(f"rotate   : {t_rot:6.2f}s  {n / t_rot:9,.0f} ops/s  "
          f"(decrypt+re-encrypt per record)")
    overhead = sum(len(t) for t in tokens[:1000]) / 1000 - payload / n
    print(f"overhead : ~{overhead:.0f} B/record (header 72 B + one 32 B chunk tag)")


if __name__ == "__main__":
    bench(int(sys.argv[1]) if len(sys.argv) > 1 else N)
