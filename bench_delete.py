"""Benchmark + reclamation evidence: point deletes vs range delete.

Deletes the same 100,000 contiguous keys from a 200,000-key index both
ways and reports wall time, temporary memory (tracemalloc peak), and
capacity/occupancy statistics. Also proves reclaimed blocks are really
garbage-collected and that later inserts reuse freed capacity instead
of growing linearly.
"""

import gc
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sorted_index import SortedIndex, _Block

N = 200_000
LO, HI = 50_000, 150_000  # delete 100k contiguous keys
BLOCK = 1024


def live_blocks():
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, _Block))


def build():
    return SortedIndex.bulk_load(((k, k) for k in range(N)),
                                 block_capacity=BLOCK)


def fmt_stats(s):
    return (f"size={s['size']:,} capacity={s['capacity']:,} "
            f"blocks={s['blocks']} fragmentation={s['fragmentation']:,}")


def main():
    print(f"index: {N:,} keys, block capacity {BLOCK}; "
          f"deleting [{LO:,}, {HI:,}) = {HI - LO:,} keys\n")

    # ---------------- reclamation evidence (range delete) ----------------
    idx = build()
    print("[range delete] before :", fmt_stats(idx.stats()),
          f"| live _Block objects: {live_blocks()}")
    tracemalloc.start()
    t0 = time.perf_counter()
    removed = idx.delete_range(LO, HI)
    dt_range = time.perf_counter() - t0
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"[range delete] removed={removed:,} time={dt_range * 1e3:.3f} ms "
          f"temp_mem_peak={peak / 1024:.1f} KiB")
    print("[range delete] after  :", fmt_stats(idx.stats()),
          f"| live _Block objects: {live_blocks()}")

    # inserts after reclamation: capacity must not grow linearly with
    # reinserted keys that land in surviving blocks' free slots
    idx2 = build()
    doomed = [k for k in range(N) if k % 4 == 1]  # 50k point deletes
    for k in doomed:
        idx2.discard(k)
    cap_mid = idx2.stats()["capacity"]
    for k in doomed:  # 50k reinserts into freed slots
        idx2.put(k, k)
    cap_end = idx2.stats()["capacity"]
    print(f"\n[reuse] 50k point deletes -> capacity {cap_mid:,}; "
          f"reinsert 50k -> capacity {cap_end:,} "
          f"(growth: {cap_end - cap_mid:,} slots)")

    # ---------------- point-by-point delete comparison ----------------
    idx = build()
    print("\n[point delete] before :", fmt_stats(idx.stats()))
    tracemalloc.start()
    t0 = time.perf_counter()
    for k in range(LO, HI):
        idx.delete(k)
    dt_point = time.perf_counter() - t0
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"[point delete] removed=100,000 time={dt_point * 1e3:.1f} ms "
          f"temp_mem_peak={peak / 1024:.1f} KiB")
    print("[point delete] after  :", fmt_stats(idx.stats()))

    print(f"\nspeedup of range delete: {dt_point / max(dt_range, 1e-9):,.0f}x")


if __name__ == "__main__":
    main()
