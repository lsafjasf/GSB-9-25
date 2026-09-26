"""Timing comparison: full-copy snapshots vs. incremental (shared) snapshots.

Scenario: a large nested dataset, a burst of small mutations, then a
snapshot. Repeated for many rounds. The full-copy baseline deep-copies the
entire dataset per snapshot; VersionedStore only copies the changed trie
paths, so its cost tracks the change size, not the dataset size.
"""

import copy
import time

from versioned_store import VersionedStore, live_node_count

ROUNDS = 200        # snapshots per run
CHANGES = 100       # mutations between two snapshots


def build_dataset(n):
    return {f"user:{i}": {"age": i % 100, "tags": ["a", "b"], "score": i}
            for i in range(n)}


def bench_full_copy(n):
    state = build_dataset(n)
    snapshots = []
    start = time.perf_counter()
    for r in range(ROUNDS):
        for c in range(CHANGES):
            state[f"user:{(r * CHANGES + c) % n}"]["age"] = c
        snapshots.append(copy.deepcopy(state))
    return time.perf_counter() - start, snapshots


def bench_incremental(n):
    store = VersionedStore.from_dict(build_dataset(n))
    versions = []
    start = time.perf_counter()
    for r in range(ROUNDS):
        for c in range(CHANGES):
            store.set((f"user:{(r * CHANGES + c) % n}", "age"), c)
        versions.append(store.snapshot())
    return time.perf_counter() - start, store, versions


def main():
    sizes = [10_000, 50_000, 200_000]
    print(f"{'dataset size':>14} | {'full-copy':>12} | {'incremental':>12} | "
          f"{'speedup':>8}   ({ROUNDS} snapshots x {CHANGES} changes each)")
    print("-" * 74)
    last_store = None
    for n in sizes:
        t_full, _ = bench_full_copy(n)
        t_inc, last_store, _ = bench_incremental(n)
        print(f"{n:>14,} | {t_full:>10.3f}s | {t_inc:>10.3f}s | "
              f"{t_full / t_inc:>7.1f}x")
    print()
    print(f"live HAMT nodes after incremental run (n={sizes[-1]:,}, "
          f"{ROUNDS} versions kept): {live_node_count():,}")
    print(f"full-copy baseline would hold ~{ROUNDS * sizes[-1]:,} row copies "
          f"for the same {ROUNDS} snapshots")
    # keep the store referenced until after the node count is printed
    assert last_store is not None


if __name__ == "__main__":
    main()
