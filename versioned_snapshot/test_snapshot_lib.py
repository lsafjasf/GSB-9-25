"""snapshot_lib 的单元测试：隔离性、回退、diff、回收、边界情形。"""

import gc
import unittest

from snapshot_lib import VersionedStore, count_nodes


def make_initial():
    return {
        "user": {"name": "alice", "tags": ["a", "b"], "addr": {"city": "BJ", "zip": "100000"}},
        "config": {"debug": False, "limits": {"cpu": 4, "mem": 16}},
        "items": [1, 2, 3],
    }


class TestIsolation(unittest.TestCase):
    """快照与后续写入强隔离：各版本视图互不污染。"""

    def test_writes_after_snapshot_do_not_leak(self):
        s = VersionedStore(make_initial())
        v0 = s.snapshot()
        s.set(("user", "name"), "bob")
        s.set(("items", 0), 999)
        s.delete(("config", "debug"))
        v1 = s.snapshot()

        # v0 视图不受 v1 之前/之后的任何写入影响
        self.assertEqual(s.view(v0), make_initial())
        s.set(("user", "addr", "city"), "SH")  # v1 之后再写
        self.assertEqual(s.view(v0), make_initial())
        self.assertEqual(s.view(v1)["user"]["name"], "bob")
        self.assertEqual(s.view(v1)["items"], [999, 2, 3])
        self.assertNotIn("debug", s.view(v1)["config"])
        self.assertEqual(s.view(v1)["user"]["addr"]["city"], "BJ")  # 深层不被污染

    def test_checkout_then_modify_does_not_pollute_other_versions(self):
        s = VersionedStore(make_initial())
        v0 = s.snapshot()
        s.set(("user", "name"), "bob")
        v1 = s.snapshot()

        s.checkout(v0)  # 回退到 v0 后继续修改
        s.set(("user", "name"), "carol")
        s.set(("config", "limits", "cpu"), 99)
        v2 = s.snapshot()

        self.assertEqual(s.view(v0)["user"]["name"], "alice")  # v0 不变
        self.assertEqual(s.view(v1)["user"]["name"], "bob")    # v1 不变
        self.assertEqual(s.view(v1)["config"]["limits"]["cpu"], 4)
        self.assertEqual(s.view(v2)["user"]["name"], "carol")
        self.assertEqual(s.view(v2)["config"]["limits"]["cpu"], 99)

    def test_mutating_returned_value_object_does_not_leak(self):
        # 快照后 set 进去的对象若之后被外部原地修改，属于调用方责任；
        # 这里验证库内部的 freeze 转换隔离了传入结构。
        s = VersionedStore({})
        payload = {"nested": {"x": 1}}
        s.set(("k",), payload)
        v0 = s.snapshot()
        payload["nested"]["x"] = 2  # 外部原地改
        self.assertEqual(s.view(v0)["k"]["nested"]["x"], 1)


class TestScenarios(unittest.TestCase):
    def test_consecutive_snapshots(self):
        s = VersionedStore({"n": 0})
        ids = []
        for i in range(1, 6):
            s.set(("n",), i)
            ids.append(s.snapshot())
        self.assertEqual(ids, [1, 2, 3, 4, 5])
        for i, v in enumerate(ids, start=1):
            self.assertEqual(s.view(v)["n"], i)
        self.assertEqual(s.view(0)["n"], 0)

    def test_snapshot_without_changes(self):
        s = VersionedStore(make_initial())
        v0 = s.snapshot()
        v1 = s.snapshot()  # 无变化
        v2 = s.snapshot()
        self.assertTrue(s.diff(v0, v2).empty)
        self.assertEqual(s.view(v0), s.view(v2))
        # 无变化快照应共享同一根节点：零额外节点
        self.assertEqual(count_nodes(s._versions[v0]), s.reachable_nodes())
        self.assertEqual(s.view(v1), make_initial())

    def test_revert_to_initial_version(self):
        s = VersionedStore(make_initial())
        v0 = 0
        s.set(("user", "name"), "bob")
        s.delete(("items", 0))
        s.snapshot()
        s.checkout(v0)
        self.assertEqual(s.head_view(), make_initial())
        s.set(("fresh",), True)  # 回退后还能继续写
        self.assertEqual(s.head_view()["fresh"], True)
        self.assertNotIn("fresh", s.view(v0))

    def test_deep_nested_modification(self):
        s = VersionedStore(make_initial())
        v0 = s.snapshot()
        s.set(("user", "addr", "city"), "SH")
        s.set(("config", "limits", "mem"), 32)
        s.set(("items", 2), 300)
        v1 = s.snapshot()
        d = s.diff(v0, v1)
        self.assertEqual(d.modified[("user", "addr", "city")], ("BJ", "SH"))
        self.assertEqual(d.modified[("config", "limits", "mem")], (16, 32))
        self.assertEqual(d.modified[("items", 2)], (3, 300))
        self.assertEqual(len(d.modified), 3)
        self.assertTrue(not d.added and not d.removed)


class TestDiff(unittest.TestCase):
    def test_added_removed_modified(self):
        s = VersionedStore({"a": 1, "b": {"x": 1, "y": 2}, "lst": [1, 2]})
        v0 = s.snapshot()
        s.set(("a",), 10)                 # modified
        s.set(("b", "z"), 3)              # added (nested)
        s.delete(("b", "x"))              # removed (nested)
        s.set(("c",), "new")              # added
        s.set(("lst", 0), 100)            # modified in list
        s.set(("lst", 2), 3)              # appended
        v1 = s.snapshot()

        d = s.diff(v0, v1)
        self.assertEqual(d.added, {("b", "z"): 3, ("c",): "new", ("lst", 2): 3})
        self.assertEqual(d.removed, {("b", "x"): 1})
        self.assertEqual(d.modified, {("a",): (1, 10), ("lst", 0): (1, 100)})

    def test_diff_of_identical_versions_is_empty(self):
        s = VersionedStore(make_initial())
        v0 = s.snapshot()
        self.assertTrue(s.diff(v0, v0).empty)


class TestVersionReclaim(unittest.TestCase):
    """删除中间版本后回收其独占数据，剩余版本保持正确。"""

    def test_delete_middle_version_reclaims_exclusive_nodes(self):
        s = VersionedStore(make_initial())
        v0 = s.snapshot()
        # v1 独占一批节点：深层路径复制产生的新容器
        s.set(("user", "addr", "city"), "SH")
        s.set(("config", "limits", "cpu"), 8)
        v1 = s.snapshot()
        v1_exclusive_view = s.view(v1)
        # v2 在 v1 基础上再改，与 v1 共享大部分节点
        s.set(("user", "name"), "bob")
        v2 = s.snapshot()

        before = s.live_tracked_nodes()
        s.delete_version(v1)
        gc.collect()
        after = s.live_tracked_nodes()

        # v1 的独占节点已被回收
        self.assertLess(after, before)
        # 存活节点数 == 剩余版本 + 工作副本实际可达节点数（无泄漏、无悬空）
        self.assertEqual(after, s.reachable_nodes())

        # 剩余版本视图仍然正确
        self.assertEqual(s.view(v0), make_initial())
        expected_v2 = v1_exclusive_view
        expected_v2["user"]["name"] = "bob"
        self.assertEqual(s.view(v2), expected_v2)
        with self.assertRaises(KeyError):
            s.view(v1)

    def test_reclaim_keeps_shared_nodes_alive(self):
        s = VersionedStore(make_initial())
        v0 = s.snapshot()
        shared_root = s._versions[v0]
        s.set(("user", "name"), "bob")
        v1 = s.snapshot()
        # 删除 v0：v1 仍共享 v0 的大部分子树，这些节点不能被回收
        s.delete_version(v0)
        gc.collect()
        self.assertEqual(s.view(v1)["user"]["addr"], {"city": "BJ", "zip": "100000"})
        self.assertEqual(s.view(v1)["config"], make_initial()["config"])
        self.assertEqual(s.live_tracked_nodes(), s.reachable_nodes())
        self.assertGreater(count_nodes(s._versions[v1]), 1)
        del shared_root

    def test_delete_all_but_one_version(self):
        s = VersionedStore({"n": 0})
        keep = None
        for i in range(1, 11):
            s.set(("n",), i)
            keep = s.snapshot()
        for v in list(s.versions()):
            if v != keep:
                s.delete_version(v)
        gc.collect()
        self.assertEqual(s.versions(), [keep])
        self.assertEqual(s.view(keep)["n"], 10)
        self.assertEqual(s.live_tracked_nodes(), s.reachable_nodes())


class TestErrors(unittest.TestCase):
    def test_unknown_version(self):
        s = VersionedStore({})
        with self.assertRaises(KeyError):
            s.checkout(99)
        with self.assertRaises(KeyError):
            s.delete_version(99)
        with self.assertRaises(KeyError):
            s.diff(0, 99)

    def test_delete_missing_key_raises(self):
        s = VersionedStore({"a": 1})
        with self.assertRaises(KeyError):
            s.delete(("nope",))


if __name__ == "__main__":
    unittest.main(verbosity=2)
