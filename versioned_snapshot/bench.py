"""全量复制快照 vs 增量（COW 结构共享）快照的耗时/内存对比。

模型：一个含 N 个键的深层状态，每轮修改 K 个键后做一次快照，共 V 个版本。
- 全量方案：每轮 copy.deepcopy 整份状态。
- 增量方案：VersionedStore，路径复制，快照 O(1)，修改 O(路径长)。
"""

import copy
import gc
import sys
import time
import tracemalloc

from snapshot_lib import VersionedStore


def build_state(n):
    # 两层结构：n//100 个分桶，每桶 100 个键，模拟较大状态
    return {f"bucket_{i}": {f"key_{j}": j for j in range(100)} for i in range(n // 100)}


def mutate_keys(version, k):
    # 每轮修改 k 个分散的键
    return [(f"bucket_{(version * 7 + i * 13) % NB}", f"key_{i % 100}") for i in range(k)]


def bench_full_copy(state, versions, k):
    snapshots = [state]
    t0 = time.perf_counter()
    cur = state
    for v in range(1, versions + 1):
        cur = copy.deepcopy(cur)  # 先改再整份快照
        for path in mutate_keys(v, k):
            cur[path[0]][path[1]] = v
        snapshots.append(cur)
    dt = time.perf_counter() - t0
    return dt, snapshots


def bench_incremental(state, versions, k):
    store = VersionedStore(state, track_nodes=False)
    store.snapshot()
    t0 = time.perf_counter()
    for v in range(1, versions + 1):
        for path in mutate_keys(v, k):
            store.set(path, v)
        store.snapshot()
    dt = time.perf_counter() - t0
    return dt, store


def deep_size(obj, seen=None):
    """递归估算对象图占用内存（共享节点只算一次）。"""
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(deep_size(k, seen) + deep_size(v, seen) for k, v in obj.items())
    elif isinstance(obj, (list, tuple)):
        size += sum(deep_size(i, seen) for i in obj)
    return size


NB = 0  # 桶数，mutate_keys 用
if __name__ == "__main__":
    versions, k = 50, 10  # 50 个版本，每轮改 10 个键
    print(f"每版本修改 {k} 个键，共 {versions} 个版本\n")
    header = f"{'总键数':>10} | {'全量复制耗时':>14} | {'增量快照耗时':>14} | {'加速比':>8} | {'全量内存':>12} | {'增量内存':>12}"
    print(header)
    print("-" * len(header))
    for n in (1_000, 10_000, 100_000, 500_000):
        NB = n // 100
        state = build_state(n)

        gc.collect()
        t_full, snaps = bench_full_copy(state, versions, k)
        mem_full = sum(deep_size(s) for s in snaps)
        del snaps
        gc.collect()

        t_inc, store = bench_incremental(state, versions, k)
        mem_inc = deep_size([store._versions[v] for v in store.versions()])
        del store
        gc.collect()

        print(
            f"{n:>10,} | {t_full*1000:>12.1f}ms | {t_inc*1000:>12.2f}ms | "
            f"{t_full/t_inc:>7.0f}x | {mem_full/1e6:>10.1f}MB | {mem_inc/1e6:>10.2f}MB"
        )

    # 增量方案下单次快照本身的开销（应为常数级）
    print("\n增量方案：单次 snapshot() 调用耗时（与数据量无关）")
    for n in (10_000, 500_000):
        NB = n // 100
        store = VersionedStore(build_state(n), track_nodes=False)
        store.snapshot()
        t0 = time.perf_counter()
        for _ in range(10_000):
            store.snapshot()
        dt = (time.perf_counter() - t0) / 10_000
        print(f"  N={n:>7,}: {dt*1e6:.2f} µs/次")
