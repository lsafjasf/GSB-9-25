#!/usr/bin/env python3
"""Measure snapshot resource behaviour of MemoryIndex.

The index table is first driven to a steady capacity (no rehashing during
measurement).  We then isolate three numbers:

1. drift caused by thousands of repeated long traversals  (must be ~0);
2. the cost of ONE open snapshot view                      (single bounded copy);
3. memory returning to baseline after the snapshot closes (no leak).

Usage:  python3 bench/memory_benchmark.py
"""

import gc
import os
import sys
import tracemalloc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory_index import MemoryIndex  # noqa: E402

ROWS = 5000
WARMUP = 50
MEASURED = 3000
GROWTH_BUDGET_BYTES = 256 * 1024  # generous headroom; observed drift is ~KiB


def format_bytes(value):
    if abs(value) < 1024:
        return "%d B" % value
    return "%.1f KiB" % (value / 1024.0)


def drain(index, expected_open_before=0):
    count = 0
    iterator = index.items()
    assert index.active_iterators() == expected_open_before + 1
    for pair in iterator:
        count += 1
    del pair
    assert count == ROWS
    assert index.active_iterators() == expected_open_before
    return iterator


def main():
    index = MemoryIndex(8192)
    for key in range(ROWS):
        index.insert(key, "value-%d" % key)
    index.compact()  # settle table capacity before measurement begins

    tracemalloc.start()

    def traced():
        current, _peak = tracemalloc.get_traced_memory()
        return current

    for _ in range(WARMUP):
        drain(index)
    gc.collect()
    baseline = traced()

    for _ in range(MEASURED):
        drain(index)
    gc.collect()
    after_traversals = traced()
    traversal_drift = after_traversals - baseline

    snapshot_iterator = index.items()
    gc.collect()
    with_snapshot = traced()
    snapshot_view_bytes = with_snapshot - after_traversals

    # Traversing/abandoning more iterators while one stays open must not leak.
    for _ in range(500):
        drain(index, expected_open_before=1)
    gc.collect()
    nested_drift = traced() - with_snapshot

    snapshot_iterator.close()
    del snapshot_iterator
    gc.collect()
    after_close = traced()
    close_return = after_close - baseline

    tracemalloc.stop()

    print("MemoryIndex snapshot resource benchmark")
    print("rows per table             : %d" % ROWS)
    print("measured traversals        : %d" % MEASURED)
    print("open iterators after run   : %d" % index.active_iterators())
    print("steady-state baseline      : %s" % format_bytes(baseline))
    print("memory after %d traversals : %s"
          % (MEASURED, format_bytes(after_traversals)))
    print("  -> drift                 : %s (%.2f bytes/traversal)"
          % (format_bytes(traversal_drift),
             traversal_drift / MEASURED))
    print("one open snapshot view     : %s (single bounded copy)"
          % format_bytes(snapshot_view_bytes))
    print("drift from 500 nested      : %s"
          % format_bytes(nested_drift))
    print("memory after snapshot close: %s (delta vs baseline %s)"
          % (format_bytes(after_close), format_bytes(close_return)))

    assert index.active_iterators() == 0
    assert traversal_drift < GROWTH_BUDGET_BYTES, (
        "traversals accumulated %s" % format_bytes(traversal_drift)
    )
    assert nested_drift < GROWTH_BUDGET_BYTES
    assert close_return < GROWTH_BUDGET_BYTES
    print("\nPASS: long traversals do not grow memory; snapshot released "
          "on close.")


if __name__ == "__main__":
    main()
