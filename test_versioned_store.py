"""Isolation, rollback, diff and reclamation tests for VersionedStore."""

import unittest

from versioned_store import VersionedStore, live_node_count


def build_store():
    """A store with a nested structure and three committed versions."""
    s = VersionedStore()
    s.set(("users", "alice", "age"), 30)
    s.set(("users", "alice", "tags"), ["dev"])
    s.set(("users", "bob", "age"), 25)
    s.set(("config", "db", "host"), "localhost")
    v0 = s.snapshot()  # initial

    s.set(("users", "alice", "age"), 31)            # modify
    s.set(("users", "carol", "age"), 40)            # add
    s.delete(("users", "bob"))                      # delete
    v1 = s.snapshot()

    s.set(("config", "db", "port"), 5432)
    v2 = s.snapshot()
    return s, v0, v1, v2


class TestSnapshots(unittest.TestCase):
    def test_consecutive_snapshots(self):
        s = VersionedStore()
        ids = [s.snapshot() for _ in range(5)]
        for i in range(5):
            s.set(("n",), i)
            ids.append(s.snapshot())
        self.assertEqual(len(set(ids)), len(ids))
        for i in range(5):
            self.assertEqual(s.to_dict(ids[5 + i]), {"n": i})
        for i in ids[:5]:
            self.assertEqual(s.to_dict(i), {})

    def test_snapshot_without_changes(self):
        s, v0, v1, _ = build_store()
        before = live_node_count()
        v_extra = s.snapshot()  # no writes since v2
        self.assertEqual(live_node_count(), before)  # zero new nodes
        self.assertEqual(s.diff(v_extra, s.versions()[-2]),
                         {"added": [], "removed": [], "modified": []})
        self.assertEqual(s.to_dict(v_extra), s.to_dict(s.versions()[-2]))
        # unused vars sanity
        self.assertNotEqual(s.to_dict(v0), s.to_dict(v1))

    def test_rollback_to_initial_version(self):
        s, v0, _, _ = build_store()
        s.set(("junk",), 1)
        s.checkout(v0)
        self.assertEqual(s.to_dict(), {
            "users": {"alice": {"age": 30, "tags": ["dev"]},
                      "bob": {"age": 25}},
            "config": {"db": {"host": "localhost"}},
        })
        # and we can continue writing from the rolled-back state
        s.set(("users", "dave", "age"), 50)
        self.assertEqual(s.get(("users", "dave", "age")), 50)
        self.assertEqual(s.get(("users", "alice", "age")), 30)


class TestIsolation(unittest.TestCase):
    def test_versions_do_not_pollute_each_other(self):
        s, v0, v1, v2 = build_store()
        expected = {v: s.to_dict(v) for v in (v0, v1, v2)}

        # Roll back to the initial version and write aggressively.
        s.checkout(v0)
        s.set(("users", "alice", "age"), 999)
        s.delete(("config", "db", "host"))
        s.set(("users", "alice", "tags"), ["hacked"])
        v3 = s.snapshot()

        # Every previously committed view must be byte-for-byte intact.
        for v, snap in expected.items():
            self.assertEqual(s.to_dict(v), snap, f"version {v} polluted")
        # The new version reflects only the branch from v0.
        self.assertEqual(s.get(("users", "alice", "age"), v3), 999)
        self.assertEqual(s.get(("users", "alice", "age"), v0), 30)
        self.assertEqual(s.get(("users", "alice", "age"), v1), 31)

    def test_deep_nested_modification_isolation(self):
        s = VersionedStore()
        s.set(("a", "b", "c", "d", "e"), {"deep": [1, 2, {"x": 1}]})
        v0 = s.snapshot()
        s.set(("a", "b", "c", "d", "e", "deep"), ["changed"])
        s.set(("a", "b", "c", "new"), True)
        v1 = s.snapshot()
        self.assertEqual(s.get(("a", "b", "c", "d", "e", "deep"), v0),
                         [1, 2, {"x": 1}])
        self.assertEqual(s.get(("a", "b", "c", "d", "e", "deep"), v1),
                         ["changed"])
        self.assertNotIn(("a", "b", "c", "new"),
                         [p for p in s.diff(v0, v0)["added"]])

    def test_mutable_leaf_isolation(self):
        s = VersionedStore()
        payload = {"items": [1, 2, 3]}
        s.set(("k",), payload)
        v0 = s.snapshot()
        payload["items"].append(999)          # mutate caller's object
        got = s.get(("k",), v0)
        got["items"].append(-1)               # mutate returned view
        self.assertEqual(s.get(("k",), v0), {"items": [1, 2, 3]})


class TestDiff(unittest.TestCase):
    def test_diff_added_removed_modified(self):
        s, v0, v1, v2 = build_store()
        d = s.diff(v0, v1)
        self.assertEqual(d["added"], [("users", "carol", "age")])
        self.assertEqual(d["removed"], [("users", "bob", "age")])
        self.assertEqual(d["modified"], [("users", "alice", "age")])
        d2 = s.diff(v1, v2)
        self.assertEqual(d2["added"], [("config", "db", "port")])
        self.assertEqual(d2["removed"], [])
        self.assertEqual(d2["modified"], [])
        # diff is symmetric for added/removed
        d3 = s.diff(v1, v0)
        self.assertEqual(d3["added"], d["removed"])
        self.assertEqual(d3["removed"], d["added"])


class TestReclamation(unittest.TestCase):
    def test_delete_version_reclaims_exclusive_nodes(self):
        s = VersionedStore()
        s.set(("base",), 0)
        v0 = s.snapshot()
        for i in range(1000):
            s.set(("v1_only", i), i)
        v1 = s.snapshot()
        for i in range(1000):
            s.set(("v2_only", i), i)
        v2 = s.snapshot()

        full = live_node_count()
        s.delete_version(v1)
        after_v1 = live_node_count()
        self.assertLess(after_v1, full)          # v1-exclusive nodes freed
        s.delete_version(v2)
        s.checkout(v0)  # release the working root, which pinned v2's tree
        after_v2 = live_node_count()
        self.assertLess(after_v2, after_v1)      # v2-exclusive nodes freed

        # Remaining version is still fully correct.
        self.assertEqual(s.to_dict(v0), {"base": 0})
        s.set(("resumed",), True)
        self.assertEqual(s.to_dict(), {"base": 0, "resumed": True})
        with self.assertRaises(KeyError):
            s.to_dict(v1)

    def test_shared_nodes_survive_deletion(self):
        s = VersionedStore()
        s.set(("shared", "sub"), [1, 2, 3])
        v0 = s.snapshot()
        s.set(("extra",), 1)
        v1 = s.snapshot()
        s.delete_version(v0)  # v1 still needs the shared subtree
        self.assertEqual(s.get(("shared", "sub"), v1), [1, 2, 3])
        self.assertEqual(s.get(("extra",), v1), 1)


class TestFieldDiff(unittest.TestCase):
    def test_entries_carry_old_and_new_values(self):
        s, v0, v1, _ = build_store()
        entries = s.diff_fields(v0, v1)
        self.assertEqual(entries, [
            {"path": ("users", "alice", "age"), "op": "modified",
             "old": 30, "new": 31},
            {"path": ("users", "bob", "age"), "op": "removed", "old": 25},
            {"path": ("users", "carol", "age"), "op": "added", "new": 40},
        ])

    def test_every_entry_checkable_field_by_field(self):
        s, v0, v1, _ = build_store()
        for e in s.diff_fields(v0, v1):
            if e["op"] in ("removed", "modified"):
                self.assertEqual(s.get(e["path"], v0), e["old"])
            if e["op"] in ("added", "modified"):
                self.assertEqual(s.get(e["path"], v1), e["new"])

    def test_replay_entries_reproduces_target_version(self):
        s, v0, _, v2 = build_store()
        entries = s.diff_fields(v0, v2)
        s.checkout(v0)
        s.apply_entries(entries)
        self.assertEqual(s.to_dict(), s.to_dict(v2))

    def test_replay_handles_subtree_replaced_by_leaf_and_back(self):
        s = VersionedStore()
        s.set(("a", "b", "c"), {"x": 1})
        v0 = s.snapshot()
        s.set(("a", "b"), 42)          # subtree -> leaf
        v1 = s.snapshot()
        s.set(("a", "b", "c"), {"x": 2})  # leaf -> subtree
        v2 = s.snapshot()
        for src, dst in ((v0, v1), (v1, v2), (v2, v0)):
            s.checkout(src)
            s.apply_entries(s.diff_fields(src, dst))
            self.assertEqual(s.to_dict(), s.to_dict(dst))


class TestIncrementalRollback(unittest.TestCase):
    def test_rollback_matches_full_rebuild_for_every_version(self):
        s, v0, v1, v2 = build_store()
        s.set(("junk", "deep"), [1, 2, 3])
        s.delete(("users", "alice"))
        for v in (v0, v1, v2):
            result = s.rollback_to(v)
            self.assertTrue(result["verified"])
            self.assertEqual(s.to_dict(), s.to_dict(v))

    def test_rollback_replays_only_the_difference(self):
        s = VersionedStore.from_dict(
            {f"k{i}": {"v": i} for i in range(5000)})
        v0 = s.snapshot()
        s.set(("k1", "v"), -1)
        s.set(("k2", "v"), -2)
        s.delete(("k3", "v"))
        before = live_node_count()
        result = s.rollback_to(v0)
        self.assertEqual(result["replayed"], 3)          # only 3 fields
        self.assertLess(live_node_count() - before, 100)  # not a rebuild
        self.assertEqual(s.to_dict(), s.to_dict(v0))

    def test_rollback_to_unknown_version_raises(self):
        s, _, _, _ = build_store()
        with self.assertRaises(KeyError):
            s.rollback_to(999)


class TestRetention(unittest.TestCase):
    def make_history(self):
        """10 versions, one hour apart, each adding one exclusive field."""
        s = VersionedStore()
        s.set(("base",), 0)
        versions = []
        for i in range(10):
            s.set((f"field_{i}",), i)
            versions.append(s.snapshot(at=1_000_000 + i * 3600))
        return s, versions

    def test_plan_requires_a_policy(self):
        s, _ = self.make_history()
        with self.assertRaises(ValueError):
            s.retention_plan()

    def test_count_and_age_policy_with_impact_scope(self):
        s, versions = self.make_history()
        now = 1_000_000 + 9 * 3600 + 1  # just after the last snapshot
        plan = s.retention_plan(max_count=3, max_age=4 * 3600, now=now)
        dropped = [e["version"] for e in plan["drop"]]
        kept = [e["version"] for e in plan["keep"]]
        # newest 3 by count, plus v6 (age < 4h); v0..v5 dropped
        self.assertEqual(dropped, versions[:6])
        self.assertEqual(kept, versions[6:])
        self.assertEqual(plan["impact"]["versions_dropped"], 6)
        self.assertGreater(plan["impact"]["nodes_freed"], 0)
        # dry-run changed nothing
        self.assertEqual(s.versions(), versions)

    def test_cleanup_never_drops_pinned_or_latest(self):
        s, versions = self.make_history()
        s.pin(versions[0])
        s.pin(versions[0])
        s.pin(versions[4])
        plan = s.apply_retention(max_count=1,
                                 now=1_000_000 + 9 * 3600 + 1)
        dropped = [e["version"] for e in plan["drop"]]
        self.assertNotIn(versions[0], dropped)   # pinned
        self.assertNotIn(versions[4], dropped)   # pinned
        self.assertNotIn(versions[-1], dropped)  # latest
        self.assertEqual(s.refs(versions[0]), 2)
        # predicted impact matches what was actually reclaimed
        self.assertEqual(plan["impact"]["nodes_freed"],
                         plan["impact"]["nodes_freed_actual"])

    def test_cleanup_preserves_surviving_versions(self):
        s, versions = self.make_history()
        before = {v: s.to_dict(v) for v in versions}
        s.pin(versions[2])
        s.apply_retention(max_count=2, now=1_000_000 + 9 * 3600 + 1)
        for v in s.versions():
            self.assertEqual(s.to_dict(v), before[v])
        # rollback to a surviving pinned version still works
        result = s.rollback_to(versions[2])
        self.assertTrue(result["verified"])

    def test_pinned_version_cannot_be_deleted(self):
        s, versions = self.make_history()
        s.pin(versions[3])
        with self.assertRaises(ValueError):
            s.delete_version(versions[3])
        s.unpin(versions[3])
        s.delete_version(versions[3])  # now it works
        with self.assertRaises(ValueError):
            s.unpin(versions[0])       # never pinned


if __name__ == "__main__":
    unittest.main(verbosity=2)
