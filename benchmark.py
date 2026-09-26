"""性能基准：十万条记录的构建与校验耗时。python3 benchmark.py [N]"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_chain import GENESIS, AuditLog, make_record, serialize_record, verify_records

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000

tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "bench.jsonl")

# 1) 构建：直接批量写文件（避免 10 万次 fsync 干扰校验计时；追加接口本身另测）
t0 = time.perf_counter()
recs, prev = [], GENESIS
for i in range(N):
    r = make_record(i, prev, {"op": "transfer", "from": f"u{i}", "to": f"u{i+1}",
                              "amount": i, "ts": 1700000000 + i})
    recs.append(r)
    prev = bytes.fromhex(r["digest"])
t1 = time.perf_counter()
with open(path, "wb") as f:
    f.write(b"".join(serialize_record(r) for r in recs))
t2 = time.perf_counter()

# 2) 从磁盘加载
log = AuditLog(path)
t3 = time.perf_counter()

# 3) 全量校验（内存中）
res = log.verify()
t4 = time.perf_counter()
assert res.ok, res

# 4) 全量校验（含磁盘加载，模拟冷启动）
log2 = AuditLog(path)
res2 = log2.verify()
t5 = time.perf_counter()
assert res2.ok, res2

# 5) 从中间位置增量校验（只验后半段）
half = N // 2
res3 = log.verify_from(half)
t6 = time.perf_counter()
assert res3.ok and res3.checked == N - half, res3

# 6) 追加接口吞吐（含 fsync，另测 1000 条）
path2 = os.path.join(tmp, "append.jsonl")
log3 = AuditLog(path2)
t7 = time.perf_counter()
for i in range(1000):
    log3.append({"i": i})
t8 = time.perf_counter()
assert log3.verify().ok

size = os.path.getsize(path)
print(f"记录数 N                : {N:,}")
print(f"文件大小                : {size/1024/1024:.1f} MiB ({size//N} B/条)")
print(f"构建摘要(内存)          : {t1-t0:.3f} s  ({N/(t1-t0):,.0f} 条/s)")
print(f"写盘                    : {t2-t1:.3f} s")
print(f"从磁盘加载              : {t3-t2:.3f} s")
print(f"全量校验(内存)          : {t4-t3:.3f} s  ({N/(t4-t3):,.0f} 条/s)")
print(f"全量校验(含加载,冷启动) : {t5-t3:.3f} s")
print(f"增量校验(后 {N-half:,} 条) : {t6-t5:.3f} s  (只扫后半段，O(N-start))")
print(f"追加接口(含fsync)       : {(t8-t7)*1000/1000:.2f} ms/条")
