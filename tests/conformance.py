"""Shared scenario drivers for the mutable-during-traversal index.

Every driver judges behaviour against the CHOSEN semantics (snapshot): once an
iterator has been created, it must visit every record live at that moment
exactly once, regardless of later inserts/deletes/compactions, and must never
observe released buffers.

The same drivers are used in two ways:

* against ``BuggyMemoryIndex`` in ``test_repro_buggy.py`` where every
  snapshot-expectation violation demonstrates one of the four production bug
  classes;
* against the fixed ``MemoryIndex`` in ``test_memory_index.py`` where zero
  violations are required.

Only the Python standard library is used.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory_index import MemoryIndex  # noqa: E402
from index_buggy import BuggyMemoryIndex  # noqa: E402

INITIAL_KEYS = list(range(12))
INITIAL_PAIRS = [(key, "v%d" % key) for key in INITIAL_KEYS]
SNAPSHOT_KEYS = set(INITIAL_KEYS)


def _fresh(factory):
    index = factory()
    for key, value in INITIAL_PAIRS:
        index.insert(key, value)
    return index


def _drain_with_mutation(index, mutate):
    """Iterate ``index.items()``; mutate after the first yielded record."""
    visited = []
    duplicates = []
    seen = set()
    error = None
    iterator = index.items()
    fired = False
    try:
        for key, value in iterator:
            if key in seen:
                duplicates.append(key)
            seen.add(key)
            visited.append(key)
            if not fired:
                fired = True
                mutate(index, key, value, iterator)
    except Exception as exc:  # stale-read / double-visit surfaces here
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
    return {"visited": visited, "duplicates": duplicates, "error": error}


def _snapshot_violations(result):
    violations = []
    if result["error"] is not None:
        violations.append("iterator error: " + result["error"])
    visited = result["visited"]
    if result["duplicates"]:
        violations.append("duplicate visits: %r" % result["duplicates"])
    visited_set = set(visited)
    missing = SNAPSHOT_KEYS - visited_set
    extra = visited_set - SNAPSHOT_KEYS
    if missing:
        violations.append("missing snapshot records: %r" % sorted(missing))
    if extra:
        violations.append("records not in snapshot: %r" % sorted(extra))
    if not result["duplicates"] and len(visited) != len(SNAPSHOT_KEYS):
        violations.append("visit count %d != snapshot size %d" % (
            len(visited), len(SNAPSHOT_KEYS)))
    return violations


def scenario_delete_current(factory):
    """Delete the record that was just yielded (the cursor slot)."""

    def mutate(index, key, value, iterator):
        index.delete(key)

    return _snapshot_violations(_drain_with_mutation(_fresh(factory), mutate))


def scenario_delete_unvisited(factory):
    """Delete a record the iterator has not reached yet."""
    index = _fresh(factory)
    order_iterator = index.items()
    order = [key for key, _ in order_iterator]
    order_iterator.close() if hasattr(order_iterator, "close") else None
    target = order[1]

    def mutate(active_index, key, value, iterator):
        active_index.delete(target)

    return _snapshot_violations(_drain_with_mutation(index, mutate))


def scenario_delete_and_reinsert(factory):
    """Delete the just-visited key, then insert the same key again."""

    def mutate(index, key, value, iterator):
        index.delete(key)
        index.insert(key, "reinserted")

    return _snapshot_violations(_drain_with_mutation(_fresh(factory), mutate))


def scenario_compact_during_iteration(factory):
    """Free/rebuild the backing table while an iterator is open."""

    def mutate(index, key, value, iterator):
        index.compact()

    return _snapshot_violations(_drain_with_mutation(_fresh(factory), mutate))


def scenario_concurrent_interleaving(factory):
    """Deterministic interleaving: deletes + inserts mixed into one traversal."""
    index = _fresh(factory)
    visited = []
    duplicates = []
    seen = set()
    error = None
    fresh_key = 1000
    iterator = index.items()
    steps = 0
    try:
        for key, value in iterator:
            if key in seen:
                duplicates.append(key)
            seen.add(key)
            visited.append(key)
            steps += 1
            if steps % 3 == 1 and key in INITIAL_KEYS:
                # Delete an already-visited record, then put it back.
                index.delete(key)
                index.insert(key, "again")
            if steps % 4 == 0:
                index.insert(fresh_key, "fresh")
                fresh_key += 1
            if steps % 5 == 0 and len(INITIAL_KEYS) - steps > 2:
                unvisited = INITIAL_KEYS[steps]
                index.delete(unvisited)
                index.insert(unvisited, "back")
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
    result = {"visited": visited, "duplicates": duplicates, "error": error}
    return _snapshot_violations(result)


SCENARIOS = {
    "delete_current": scenario_delete_current,
    "delete_unvisited": scenario_delete_unvisited,
    "delete_then_reinsert_same_key": scenario_delete_and_reinsert,
    "compact_during_iteration": scenario_compact_during_iteration,
    "concurrent_interleaved_insert_delete": scenario_concurrent_interleaving,
}


def scenario_stats_consistency(factory):
    """Exercise counters through writes/overwrites/deletes/compaction."""
    index = factory(capacity=64)
    mirror = {}
    violations = []

    def check(label):
        live_keys = list(mirror)
        stats = index.stats()
        if stats["entries"] != len(live_keys):
            violations.append(
                "%s: entries=%d but %d live records"
                % (label, stats["entries"], len(live_keys))
            )
        if len(index) != len(live_keys):
            violations.append(
                "%s: len(index)=%d but %d live records"
                % (label, len(index), len(live_keys))
            )
        if stats["entries"] > stats["capacity"]:
            violations.append(
                "%s: entries %d exceed capacity %d"
                % (label, stats["entries"], stats["capacity"])
            )
        return live_keys

    for key in range(20):
        index.insert(key, mirror.get(key))
        mirror[key] = True
    check("after_inserts")
    for _ in range(3):  # pure overwrites must not change any counter
        before = index.stats()
        for key in range(20):
            index.insert(key, "overwritten")
        after = index.stats()
        if before["entries"] != after["entries"]:
            violations.append(
                "overwrite changed entries %s -> %s"
                % (before["entries"], after["entries"])
            )
    check("after_overwrites")
    for key in range(0, 20, 2):
        index.delete(key)
        del mirror[key]
    check("after_deletes")
    for key in (2, 6):
        index.insert(key, "returned")
        mirror[key] = True
    check("after_reinserts")
    stats_before = index.stats()
    index.compact()
    stats_after = index.stats()
    check("after_compact")
    if stats_after["deleted"] != 0:
        violations.append(
            "compact left deleted=%d" % stats_after["deleted"]
        )
    if stats_before["deleted"] < 0:
        violations.append("negative deleted counter")
    index.clear()
    mirror.clear()
    check("after_clear")
    if index.stats()["deleted"] != 0:
        violations.append("clear left non-zero deleted counter")
    invariant_check = getattr(index, "check_invariants", None)
    if invariant_check is not None:
        try:
            invariant_check()
        except AssertionError as exc:
            violations.append("invariant failure: %s" % exc)
    return violations


FACTORIES = {
    "fixed": lambda capacity=16: MemoryIndex(capacity),
    "buggy": lambda capacity=16: BuggyMemoryIndex(capacity),
}
