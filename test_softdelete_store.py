"""softdelete_store 自测集：边界用例 + 恢复一致性断言 + 规模数据。
运行：python3 -m unittest test_softdelete_store -v
"""

import unittest

from softdelete_store import (
    TreeStore, ROOT, REJECT, NEAREST_ANCESTOR,
    AlreadyDeletedError, DeleteFailedError, HasLiveChildrenError,
    NotDeletedError, NotFoundError, NotInTrashError, ParentMissingError,
    StoreError,
)


def build_sample_tree():
    r"""root_a -- b1 -- c1 (type=leaf)
              \- b2 -- c2 (type=leaf)
    root_b (type=leaf)
    """
    s = TreeStore()
    s.add("root_a", kind="root")
    s.add("b1", "root_a", kind="branch")
    s.add("b2", "root_a", kind="branch")
    s.add("c1", "b1", kind="leaf", tag="x")
    s.add("c2", "b2", kind="leaf", tag="x")
    s.add("root_b", kind="leaf", tag="y")
    return s


class TestBasicsAndEmptyTree(unittest.TestCase):
    def test_empty_tree(self):
        s = TreeStore()
        self.assertEqual(s.list_live(), [])
        self.assertEqual(s.roots(), [])
        self.assertEqual(s.trash(), [])
        self.assertEqual(s.find("kind", "leaf"), [])
        for op in (lambda: s.delete("nope"),
                   lambda: s.restore("nope"),
                   lambda: s.purge("nope"),
                   lambda: s.get("nope")):
            with self.assertRaises(NotFoundError):
                op()
        self.assertTrue(s.check_invariants())

    def test_single_node_lifecycle(self):
        s = TreeStore()
        s.add("only", kind="leaf")
        before = s.snapshot()
        s.delete("only")
        self.assertFalse(s.exists("only"))
        self.assertEqual(s.list_live(), [])
        self.assertEqual([t["id"] for t in s.trash()], ["only"])
        self.assertIsNone(s.trash()[0]["original_parent"])
        s.restore("only")
        self.assertEqual(s.snapshot(), before)
        self.assertTrue(s.check_invariants())

    def test_duplicate_id_rejected(self):
        s = TreeStore()
        s.add("a")
        with self.assertRaises(StoreError):
            s.add("a")

    def test_add_under_deleted_parent_rejected(self):
        s = TreeStore()
        s.add("p")
        s.delete("p")
        with self.assertRaises(StoreError):
            s.add("child", "p")


class TestDeleteSemantics(unittest.TestCase):
    def test_repeated_delete_deterministic(self):
        s = build_sample_tree()
        s.delete("c1")
        with self.assertRaises(AlreadyDeletedError):
            s.delete("c1")
        with self.assertRaises(AlreadyDeletedError):
            s.delete("c1", cascade=True)

    def test_delete_non_leaf_requires_cascade(self):
        s = build_sample_tree()
        with self.assertRaises(HasLiveChildrenError):
            s.delete("b1")
        self.assertTrue(s.exists("b1"))  # 状态未变

    def test_normal_queries_hide_deleted(self):
        s = build_sample_tree()
        s.delete("b1", cascade=True)
        self.assertFalse(s.exists("b1"))
        self.assertFalse(s.exists("c1"))
        with self.assertRaises(NotFoundError):
            s.get("c1")
        self.assertEqual(s.children("root_a"), ["b2"])
        self.assertEqual(s.find("kind", "leaf"), ["c2", "root_b"])
        self.assertEqual(s.find("tag", "x"), ["c2"])
        self.assertEqual(s.roots(), ["root_a", "root_b"])
        self.assertTrue(s.check_invariants())

    def test_trash_view_shows_original_parent(self):
        s = build_sample_tree()
        s.delete("b1", cascade=True)
        trash = {t["id"]: t["original_parent"] for t in s.trash()}
        self.assertEqual(trash, {"b1": "root_a", "c1": "b1"})
        self.assertEqual(s.trash_roots(), ["b1"])

    def test_cascade_delete_failure_rolls_back(self):
        s = build_sample_tree()
        before = s.snapshot()
        calls = []

        def hook(nid):
            calls.append(nid)
            if len(calls) == 2:
                raise RuntimeError("注入的中途故障")

        with self.assertRaises(DeleteFailedError):
            s.delete("root_a", cascade=True, hook=hook)
        # 确定结果：全部回滚，与删除前完全一致，回收站为空
        self.assertEqual(s.snapshot(), before)
        self.assertEqual(s.trash(), [])
        self.assertEqual(s.find("tag", "x"), ["c1", "c2"])
        self.assertTrue(s.check_invariants())

    def test_delete_restore_delete_restore_cycles(self):
        s = build_sample_tree()
        before = s.snapshot()
        for _ in range(3):
            s.delete("root_a", cascade=True)
            self.assertEqual(s.roots(), ["root_b"])
            s.restore("root_a", subtree=True)
            self.assertEqual(s.snapshot(), before)
        self.assertTrue(s.check_invariants())


class TestRestoreConsistency(unittest.TestCase):
    def test_restore_leaf_identical(self):
        s = build_sample_tree()
        before = s.snapshot()
        s.delete("c1")
        s.restore("c1")
        self.assertEqual(s.snapshot(), before)
        self.assertTrue(s.check_invariants())

    def test_restore_subtree_identical(self):
        """级联删除后整棵恢复：结构、父子关系、索引与删除前完全一致。"""
        s = build_sample_tree()
        before = s.snapshot()
        live_before = s.list_live()
        s.delete("root_a", cascade=True)
        self.assertEqual(s.list_live(), ["root_b"])
        s.restore("root_a", subtree=True)
        self.assertEqual(s.snapshot(), before)
        self.assertEqual(s.list_live(), live_before)
        self.assertEqual(s.parent("c1"), "b1")
        self.assertEqual(s.children("root_a"), ["b1", "b2"])
        self.assertEqual(s.find("tag", "x"), ["c1", "c2"])
        self.assertTrue(s.check_invariants())

    def test_restore_single_keeps_children_in_trash(self):
        s = build_sample_tree()
        s.delete("b1", cascade=True)
        s.restore("b1")  # 非 subtree：只恢复自身
        self.assertTrue(s.exists("b1"))
        self.assertFalse(s.exists("c1"))
        self.assertEqual([t["id"] for t in s.trash()], ["c1"])
        self.assertEqual(s.children("b1"), [])  # c1 仍删除，普通查询不可见
        s.restore("c1")
        self.assertEqual(s.children("b1"), ["c1"])
        self.assertTrue(s.check_invariants())

    def test_restore_not_deleted_raises(self):
        s = build_sample_tree()
        with self.assertRaises(NotDeletedError):
            s.restore("c1")

    def test_unknown_policy_rejected(self):
        s = build_sample_tree()
        s.delete("c1")
        with self.assertRaises(ValueError):
            s.restore("c1", policy="bogus")


class TestMissingParentPolicies(unittest.TestCase):
    """原父节点已不存在的三种策略：ROOT / REJECT / NEAREST_ANCESTOR。
    构造：a -> b -> c，先删 c 再删 b（均非级联），再 purge b，
    使 c 的原父节点 b 彻底不存在。"""

    def make_orphan(self):
        s = TreeStore()
        s.add("a", kind="root")
        s.add("b", "a", kind="branch")
        s.add("c", "b", kind="leaf")
        s.delete("c")
        s.delete("b")
        s.purge("b", cascade=False)  # 只清除 b，c 成为孤儿
        self.assertTrue(s.in_trash("c"))
        return s

    def test_policy_root(self):
        s = self.make_orphan()
        s.restore("c", policy=ROOT)
        self.assertTrue(s.exists("c"))
        self.assertIsNone(s.parent("c"))
        self.assertIn("c", s.roots())
        self.assertTrue(s.check_invariants())

    def test_policy_reject(self):
        s = self.make_orphan()
        with self.assertRaises(ParentMissingError) as ctx:
            s.restore("c", policy=REJECT)
        self.assertIn("b", str(ctx.exception))  # 说明缺失的父节点
        self.assertTrue(s.in_trash("c"))        # 拒绝后状态不变
        self.assertTrue(s.check_invariants())

    def test_policy_nearest_ancestor(self):
        s = self.make_orphan()
        s.restore("c", policy=NEAREST_ANCESTOR)
        self.assertEqual(s.parent("c"), "a")  # 最近存在祖先是 a
        self.assertEqual(s.children("a"), ["c"])
        self.assertTrue(s.check_invariants())

    def test_policy_nearest_falls_back_to_root(self):
        s = TreeStore()
        s.add("a")
        s.add("b", "a")
        s.add("c", "b")
        s.delete("a", cascade=True)  # 整棵都在回收站
        s.restore("c", policy=NEAREST_ANCESTOR)  # 没有活祖先 -> 根
        self.assertIsNone(s.parent("c"))
        self.assertIn("c", s.roots())
        self.assertTrue(s.check_invariants())

    def test_policy_reject_when_parent_only_soft_deleted(self):
        s = TreeStore()
        s.add("a")
        s.add("b", "a")
        s.delete("a", cascade=True)
        with self.assertRaises(ParentMissingError):
            s.restore("b", policy=REJECT)
        self.assertTrue(s.in_trash("b"))

    def test_restore_subtree_reject_is_atomic(self):
        """REJECT 失败时整棵子树恢复不产生任何修改。"""
        s = self.make_orphan()
        before = s.snapshot()
        with self.assertRaises(ParentMissingError):
            s.restore("c", policy=REJECT, subtree=True)
        self.assertEqual(s.snapshot(), before)


class TestPurge(unittest.TestCase):
    def test_purge_then_restore_fails(self):
        s = build_sample_tree()
        s.delete("c1")
        s.purge("c1")
        with self.assertRaises(NotFoundError):
            s.restore("c1")
        with self.assertRaises(NotFoundError):
            s.delete("c1")
        self.assertEqual(s.trash(), [])
        self.assertTrue(s.check_invariants())

    def test_purge_live_node_rejected(self):
        s = build_sample_tree()
        with self.assertRaises(NotInTrashError):
            s.purge("c1")

    def test_purge_subtree_removes_descendants(self):
        s = build_sample_tree()
        s.delete("root_a", cascade=True)
        self.assertEqual(len(s.trash()), 5)
        n = s.purge("root_a")
        self.assertEqual(n, 5)
        self.assertEqual(s.trash(), [])
        self.assertEqual(s.list_live(), ["root_b"])
        self.assertTrue(s.check_invariants())

    def test_purge_all(self):
        s = build_sample_tree()
        s.delete("c1")
        s.delete("root_b")
        self.assertEqual(s.purge_all(), 2)
        self.assertEqual(s.trash(), [])
        self.assertTrue(s.check_invariants())

    def test_double_purge_deterministic(self):
        s = build_sample_tree()
        s.delete("c1")
        s.purge("c1")
        with self.assertRaises(NotFoundError):
            s.purge("c1")


class TestIndex(unittest.TestCase):
    def test_set_attr_updates_index(self):
        s = build_sample_tree()
        s.set_attr("c1", "tag", "z")
        self.assertEqual(s.find("tag", "x"), ["c2"])
        self.assertEqual(s.find("tag", "z"), ["c1"])
        self.assertTrue(s.check_invariants())

    def test_index_no_residue_after_delete_restore(self):
        s = build_sample_tree()
        before = s.snapshot()
        s.delete("root_a", cascade=True)
        self.assertEqual(s.find("kind", "branch"), [])
        self.assertEqual(s.find("tag", "x"), [])
        s.restore("root_a", subtree=True)
        self.assertEqual(s.snapshot()["index"], before["index"])
        self.assertTrue(s.check_invariants())


class TestDeepNesting(unittest.TestCase):
    def test_deep_chain_delete_restore(self):
        depth = 2000  # 远超默认递归限制，验证迭代实现
        s = TreeStore()
        s.add("n0", level=0)
        for i in range(1, depth):
            s.add("n%d" % i, "n%d" % (i - 1), level=i)
        before = s.snapshot()
        s.delete("n0", cascade=True)
        self.assertEqual(s.list_live(), [])
        self.assertEqual(len(s.trash()), depth)
        s.restore("n0", subtree=True)
        self.assertTrue(s.snapshot() == before, "深层链恢复后快照不一致")
        self.assertEqual(s.parent("n%d" % (depth - 1)), "n%d" % (depth - 2))
        self.assertTrue(s.check_invariants())

    def test_deep_chain_partial_restore(self):
        depth = 500
        s = TreeStore()
        s.add("n0")
        for i in range(1, depth):
            s.add("n%d" % i, "n%d" % (i - 1))
        s.delete("n100", cascade=True)
        s.restore("n100", subtree=True)
        self.assertEqual(len(s.list_live()), depth)
        self.assertTrue(s.check_invariants())


class TestScale(unittest.TestCase):
    def test_bulk_cascade_delete_restore(self):
        """上万节点：1 根 + 150 分支 x 100 叶 = 15101 节点，级联删除并整体恢复。"""
        branches, leaves = 150, 100
        s = TreeStore()
        s.add("root", kind="root")
        for b in range(branches):
            s.add("b%d" % b, "root", kind="branch", grp=b % 10)
            for l in range(leaves):
                s.add("b%d_l%d" % (b, l), "b%d" % b, kind="leaf", grp=b % 10)
        total = 1 + branches + branches * leaves
        self.assertEqual(len(s.list_live()), total)
        before = s.snapshot()

        deleted = s.delete("root", cascade=True)
        self.assertEqual(deleted, total)
        self.assertEqual(s.list_live(), [])
        self.assertEqual(len(s.trash()), total)
        self.assertEqual(s.find("kind", "leaf"), [])
        self.assertTrue(s.check_invariants())

        restored = s.restore("root", subtree=True)
        self.assertEqual(restored, total)
        self.assertTrue(s.snapshot() == before, "规模数据恢复后快照不一致")
        self.assertEqual(len(s.find("kind", "leaf")), branches * leaves)
        self.assertEqual(len(s.find("grp", 3)), (branches // 10) * (1 + leaves))
        self.assertTrue(s.check_invariants())

    def test_bulk_purge_all(self):
        s = TreeStore()
        for i in range(12000):
            s.add("n%d" % i, tag=i % 7)
        for i in range(12000):
            if i % 2 == 0:
                s.delete("n%d" % i)
        self.assertEqual(s.purge_all(), 6000)
        self.assertEqual(len(s.list_live()), 6000)
        self.assertEqual(s.trash(), [])
        self.assertTrue(s.check_invariants())


if __name__ == "__main__":
    unittest.main(verbosity=2)
