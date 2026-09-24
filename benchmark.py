"""十万条记录加解密 / 轮换性能基准：python3 benchmark.py"""

import os
import random
import time

import fieldcrypt as fc

N = 100_000
FIELDS = 3  # 每条记录 3 个敏感字段


def main():
    random.seed(42)
    store = fc.KeyStore()
    store.add(1, os.urandom(32))

    # 模拟记录：主键 + 3 个敏感字段（姓名/邮箱/手机号量级）
    records = [
        (f"user:{i}", [
            ("张" + str(i)).encode("utf-8"),
            f"user{i}@example.com".encode(),
            ("138" + str(i).zfill(8)).encode(),
        ])
        for i in range(N)
    ]
    total_bytes = sum(len(f) for _, fs in records for f in fs)

    # ---- 加密 ----
    t0 = time.perf_counter()
    encrypted = [
        (pk, [fc.encrypt(f, store, aad=pk) for f in fs])
        for pk, fs in records
    ]
    t_enc = time.perf_counter() - t0

    # ---- 解密（含完整性校验 + AAD 校验）----
    t0 = time.perf_counter()
    for pk, cts in encrypted:
        for ct in cts:
            fc.decrypt(ct, store, aad=pk)
    t_dec = time.perf_counter() - t0

    # ---- 密钥轮换 + 全量重加密 ----
    store.add(2, os.urandom(32))
    t0 = time.perf_counter()
    rotated = 0
    for pk, cts in encrypted:
        for i, ct in enumerate(cts):
            new = fc.rotate(ct, store, aad=pk)
            if new is not ct:
                cts[i] = new
                rotated += 1
    t_rot = time.perf_counter() - t0

    n_fields = N * FIELDS
    print(f"记录数:            {N:,} (每条 {FIELDS} 个加密字段, 共 {n_fields:,} 次加密)")
    print(f"明文总量:          {total_bytes / 1e6:.1f} MB")
    print(f"加密:              {t_enc:.2f} s  "
          f"({n_fields / t_enc:,.0f} 字段/s, {total_bytes / t_enc / 1e6:.1f} MB/s, "
          f"{t_enc / N * 1e6:.1f} µs/记录)")
    print(f"解密(含校验):      {t_dec:.2f} s  "
          f"({n_fields / t_dec:,.0f} 字段/s, {total_bytes / t_dec / 1e6:.1f} MB/s, "
          f"{t_dec / N * 1e6:.1f} µs/记录)")
    print(f"加解密合计:        {t_enc + t_dec:.2f} s")
    print(f"轮换重加密(全量):  {t_rot:.2f} s  "
          f"({rotated:,} 个字段, {rotated / t_rot:,.0f} 字段/s)")

    # 正确性抽查
    pk, cts = encrypted[N // 2]
    plain = [fc.decrypt(c, store, aad=pk) for c in cts]
    assert plain == list(records[N // 2][1]), "轮换后解密结果不一致"
    assert all(fc.peek_key_version(c) == 2 for _, cts in encrypted
               for c in cts)
    print("抽查:              轮换后全部字段为 v2 且解密一致 ✔")


if __name__ == "__main__":
    main()
