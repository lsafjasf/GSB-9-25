"""End-to-end verification for the diff / incremental-rollback / retention
feature iteration. Re-runnable: exits non-zero on any failed check.

    python3 verify_iteration.py
"""

import random
import time

from versioned_store import VersionedStore, live_node_count

PASS = []


def check(label, ok):
    PASS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


# ------------------------------------------------------- 1. field-level diff
def scenario_field_diff():
    print("=" * 68)
    print("1. Field-level diff: every entry checkable against its version")
    print("=" * 68)
    s = VersionedStore.from_dict({
        "users": {"alice": {"age": 30, "tags": ["dev"]},
                  "bob": {"age": 25}},
        "config": {"db": {"host": "localhost"}},
    })
    v0 = s.snapshot()
    s.set(("users", "alice", "age"), 31)       # modify
    s.set(("users", "carol", "age"), 40)       # add
    s.delete(("users", "bob"))                 # delete a subtree
    s.set(("config", "db"), "sqlite://mem")    # subtree -> leaf
    v1 = s.snapshot()

    entries = s.diff_fields(v0, v1)
    print(f"  diff_fields(v{v0}, v{v1}): {len(entries)} changed fields")
    for e in entries:
        if e["op"] == "modified":
            print(f"    modified {e['path']}: {e['old']!r} -> {e['new']!r}")
        elif e["op"] == "added":
            print(f"    added    {e['path']}: {e['new']!r}")
        else:
            print(f"    removed  {e['path']}: {e['old']!r}")

    # field-by-field cross-check against both versions
    ok = True
    for e in entries:
        if e["op"] in ("removed", "modified"):
            ok &= s.get(e["path"], v0) == e["old"]
        if e["op"] in ("added", "modified"):
            ok &= s.get(e["path"], v1) == e["new"]
    check("each entry's old/new matches get(path, v0/v1)", ok)

    # replaying the diff on top of v0 reproduces v1 exactly
    s.checkout(v0)
    s.apply_entries(entries)
    check("replay diff on v0 == full rebuild of v1",
          s.to_dict() == s.to_dict(v1))


# --------------------------------------------- 2. incremental rollback path
def scenario_incremental_rollback():
    print()
    print("=" * 68)
    print("2. Incremental rollback: replay the diff, not a full rebuild")
    print("=" * 68)
    rng = random.Random(42)
    n_records, n_versions, changes = 20_000, 30, 100
    s = VersionedStore.from_dict(
        {f"user:{i}": {"age": i % 100, "score": i} for i in range(n_records)})
    versions = []
    for r in range(n_versions):
        for _ in range(changes):
            s.set((f"user:{rng.randrange(n_records)}", "age"),
                  rng.randrange(100))
        versions.append(s.snapshot())
    print(f"  {n_records:,} records x {n_versions} versions "
          f"({changes} changes/round)")

    targets = [versions[0], versions[7], versions[15], versions[-1]]
    print(f"  {'target':>8} | {'entries replayed':>16} | "
          f"{'nodes allocated':>15} | {'== full rebuild':>15}")
    print("  " + "-" * 62)
    all_ok = True
    for t in targets:
        # dirty the working state so rollback has real work to do
        for _ in range(50):
            s.set((f"user:{rng.randrange(n_records)}", "score"), -1)
        result = s.rollback_to(t)
        same = s.to_dict() == s.to_dict(t)  # full rebuild of the target
        all_ok &= result["verified"] and same
        print(f"  v{t:>7} | {result['replayed']:>16} | "
              f"{result['nodes_allocated']:>15} | {str(same):>15}")
    check("rollback to every target matches a full rebuild", all_ok)


# ---------------------------------------------------- 3. retention policy
def scenario_retention():
    print()
    print("=" * 68)
    print("3. Retention policy: count + age, dry-run impact, pinned safety")
    print("=" * 68)
    s = VersionedStore()
    s.set(("base",), 0)
    t0 = 1_700_000_000
    versions = []
    for i in range(12):  # one snapshot per hour for 12 hours
        s.set((f"metric_{i}",), {"value": i, "series": list(range(i + 1))})
        versions.append(s.snapshot(at=t0 + i * 3600))
    now = t0 + 12 * 3600

    # v2 and v5 are still referenced by downstream consumers
    s.pin(versions[2])
    s.pin(versions[5])
    s.pin(versions[5])
    before = {v: s.to_dict(v) for v in versions}

    plan = s.retention_plan(max_count=3, max_age=4 * 3600, now=now)
    print(f"  policy: keep newest {plan['policy']['max_count']} versions "
          f"AND everything younger than "
          f"{plan['policy']['max_age'] // 3600}h; pinned always kept")
    print(f"  {'version':>8} | {'age':>6} | decision")
    print("  " + "-" * 56)
    kept_ids = {e["version"] for e in plan["keep"]}
    for v in versions:
        if v in kept_ids:
            reasons = next(e["reasons"] for e in plan["keep"]
                           if e["version"] == v)
            decision = "KEEP  (" + "; ".join(reasons) + ")"
        else:
            decision = "DROP"
        age_h = (now - s.version_info(v)["created_at"]) / 3600
        print(f"  v{v:>7} | {age_h:>5.0f}h | {decision}")
    imp = plan["impact"]
    print(f"  impact: drop {imp['versions_dropped']} versions, "
          f"keep {imp['versions_kept']}; "
          f"frees {imp['nodes_freed']:,} HAMT nodes, "
          f"{imp['nodes_remaining']:,} remain")

    check("dry-run changed nothing", s.versions() == versions)
    check("pinned versions never selected for drop",
          all(v in kept_ids for v in (versions[2], versions[5])))
    check("latest version never selected for drop",
          versions[-1] in kept_ids)

    result = s.apply_retention(max_count=3, max_age=4 * 3600, now=now)
    check("predicted impact == actually freed nodes",
          result["impact"]["nodes_freed"]
          == result["impact"]["nodes_freed_actual"])
    check("surviving versions byte-for-byte intact after cleanup",
          all(s.to_dict(v) == before[v] for v in s.versions()))
    check("pinned refs survive cleanup",
          s.refs(versions[2]) == 1 and s.refs(versions[5]) == 2)
    rb = s.rollback_to(versions[2])
    check("incremental rollback to a pinned survivor still verifies",
          rb["verified"] and s.to_dict() == before[versions[2]])
    print(f"  live HAMT nodes after cleanup: {live_node_count():,}")


def main():
    started = time.perf_counter()
    scenario_field_diff()
    scenario_incremental_rollback()
    scenario_retention()
    print()
    print("=" * 68)
    total = len(PASS)
    passed = sum(PASS)
    print(f"RESULT: {passed}/{total} checks passed "
          f"in {time.perf_counter() - started:.2f}s")
    if passed != total:
        raise SystemExit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
