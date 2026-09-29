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


class FakeClock:
    """可手动推进的假时钟，让删除时间区间筛选可确定性测试。"""

    def __init__(self, start=0):
        self.now = start

    def __call__(self):
        return self.now


def build_trash_fixture():
    r"""构造回收站数据（删除时间由假时钟控制）：
    t=100: delete("a", cascade)   -> a, a1, a2, a1x   (级联根 a)
    t=200: delete("b")            -> b                 (单删，根层)
    t=300: delete("c1")           -> c1                (原父 c 存活)
    t=400: delete("d", cascade)   -> d, d1             (级联根 d，根层)
    """
    clock = FakeClock()
    s = TreeStore(clock=clock)
    s.add("a", kind="root")
    s.add("a1", "a", kind="branch")
    s.add("a2", "a", kind="branch")
    s.add("a1x", "a1", kind="leaf")
    s.add("b", kind="leaf")
    s.add("c", kind="root")
    s.add("c1", "c", kind="leaf")
    s.add("d", kind="root")
    s.add("d1", "d", kind="leaf")
    clock.now = 100
    s.delete("a", cascade=True)
    clock.now = 200
    s.delete("b")
    clock.now = 300
    s.delete("c1")
    clock.now = 400
    s.delete("d", cascade=True)
    return s


def page_concat(store, page_size, **filters):
    """按页拉取并拼接，模拟客户端分页消费。"""
    items = []
    for page in store.trash_iter_pages(page_size, **filters):
        items.extend(page["items"])
    return items


class TestTrashPagination(unittest.TestCase):
    def test_pages_concat_equals_full_list(self):
        """核心一致性：各种页大小下，分页拼接与全量列表完全一致。"""
        s = build_trash_fixture()
        full = s.trash_query()["items"]
        self.assertEqual(len(full), 8)
        for page_size in range(1, 12):
            self.assertEqual(page_concat(s, page_size), full,
                             "page_size=%d 时拼接结果与全量不一致" % page_size)

    def test_pages_concat_equals_full_list_with_filters(self):
        """带筛选条件时，分页拼接同样与筛选后的全量一致。"""
        s = build_trash_fixture()
        for filters in ({"original_parent": None},
                        {"original_parent": "a"},
                        {"deleted_after": 150},
                        {"deleted_before": 300},
                        {"deleted_after": 150, "deleted_before": 350},
                        {"cascade_root": True},
                        {"cascade_root": False},
                        {"cascade_root": "a"}):
            full = s.trash_query(**filters)["items"]
            for page_size in (1, 2, 3):
                self.assertEqual(page_concat(s, page_size, **filters), full,
                                 "filters=%r page_size=%d 不一致"
                                 % (filters, page_size))

    def test_offset_limit_and_total(self):
        s = build_trash_fixture()
        page = s.trash_query(offset=2, limit=3)
        self.assertEqual(page["total"], 8)  # total 是筛选后总数，不受分页影响
        self.assertEqual([i["id"] for i in page["items"]],
                         [i["id"] for i in s.trash_query()["items"][2:5]])
        self.assertEqual(s.trash_query(offset=100)["items"], [])
        self.assertEqual(s.trash_query(limit=0)["items"], [])
        with self.assertRaises(ValueError):
            s.trash_query(offset=-1)
        with self.assertRaises(ValueError):
            s.trash_query(limit=-1)
        with self.assertRaises(ValueError):
            list(s.trash_iter_pages(0))

    def test_stable_order_across_mixed_deletes(self):
        """分页序按删除先后（deleted_seq），与 id 字典序无关。"""
        s = TreeStore()
        for nid in ("z1", "m1", "a1"):
            s.add(nid)
        for nid in ("z1", "m1", "a1"):
            s.delete(nid)
        self.assertEqual([i["id"] for i in s.trash_query()["items"]],
                         ["z1", "m1", "a1"])


class TestTrashFilters(unittest.TestCase):
    def test_filter_by_original_parent(self):
        s = build_trash_fixture()
        under_a = s.trash_query(original_parent="a")["items"]
        self.assertEqual({i["id"] for i in under_a}, {"a1", "a2"})
        top = s.trash_query(original_parent=None)["items"]
        self.assertEqual({i["id"] for i in top}, {"a", "b", "d"})

    def test_filter_by_deleted_time_range(self):
        s = build_trash_fixture()
        self.assertEqual({i["id"] for i in
                          s.trash_query(deleted_after=200)["items"]},
                         {"b", "c1", "d", "d1"})
        self.assertEqual({i["id"] for i in
                          s.trash_query(deleted_before=200)["items"]},
                         {"a", "a1", "a2", "a1x", "b"})
        # 闭区间端点
        self.assertEqual({i["id"] for i in s.trash_query(
            deleted_after=200, deleted_before=300)["items"]}, {"b", "c1"})
        self.assertEqual(s.trash_query(deleted_after=500)["items"], [])

    def test_filter_by_cascade_root(self):
        s = build_trash_fixture()
        roots = s.trash_query(cascade_root=True)["items"]
        self.assertEqual({i["id"] for i in roots}, {"a", "b", "c1", "d"})
        cascaded = s.trash_query(cascade_root=False)["items"]
        self.assertEqual({i["id"] for i in cascaded},
                         {"a1", "a2", "a1x", "d1"})
        by_op = s.trash_query(cascade_root="a")["items"]
        self.assertEqual({i["id"] for i in by_op}, {"a", "a1", "a2", "a1x"})

    def test_filters_compose(self):
        s = build_trash_fixture()
        items = s.trash_query(original_parent=None, deleted_after=300,
                              cascade_root=True)["items"]
        self.assertEqual([i["id"] for i in items], ["d"])


class TestTrashTreeExpansion(unittest.TestCase):
    def test_trash_children_level_by_level(self):
        s = build_trash_fixture()
        top = [i["id"] for i in s.trash_children()]
        self.assertEqual(top, ["a", "b", "c1", "d"])  # 回收站顶层
        self.assertEqual([i["id"] for i in s.trash_children("a")], ["a1", "a2"])
        self.assertEqual([i["id"] for i in s.trash_children("a1")], ["a1x"])
        self.assertEqual(s.trash_children("a1x"), [])
        with self.assertRaises(NotInTrashError):
            s.trash_children("c")  # 活节点不在回收站
        with self.assertRaises(NotFoundError):
            s.trash_children("nope")

    def test_trash_tree_nested(self):
        s = build_trash_fixture()
        forest = s.trash_tree()
        self.assertEqual([t["id"] for t in forest], ["a", "b", "c1", "d"])
        a_tree = forest[0]
        self.assertEqual([c["id"] for c in a_tree["children"]], ["a1", "a2"])
        self.assertEqual([c["id"] for c in a_tree["children"][0]["children"]],
                         ["a1x"])
        self.assertEqual(a_tree["children"][0]["children"][0]["children"], [])
        # 指定根展开
        d_tree = s.trash_tree("d")
        self.assertEqual(len(d_tree), 1)
        self.assertEqual([c["id"] for c in d_tree[0]["children"]], ["d1"])
        with self.assertRaises(NotInTrashError):
            s.trash_tree("c")

    def test_trash_tree_deep_chain_no_recursion(self):
        depth = 2000
        s = TreeStore()
        s.add("n0")
        for i in range(1, depth):
            s.add("n%d" % i, "n%d" % (i - 1))
        s.delete("n0", cascade=True)
        tree = s.trash_tree("n0")
        node, count = tree[0], 1
        while node["children"]:
            node = node["children"][0]
            count += 1
        self.assertEqual(count, depth)


class TestBatchRestore(unittest.TestCase):
    def test_batch_restore_matches_sequential(self):
        """核心一致性：批量恢复与逐个恢复的最终结构完全一致。"""
        ids = ["a1x", "a1", "a2", "a", "c1", "d1", "d", "b"]
        s1 = build_trash_fixture()
        result = s1.restore_many(ids)
        self.assertTrue(result.ok)
        self.assertEqual(result.succeeded, ids)

        s2 = build_trash_fixture()
        for nid in ids:
            s2.restore(nid)
        self.assertEqual(s1.snapshot(), s2.snapshot())
        self.assertEqual(s1.list_live(), s2.list_live())
        self.assertTrue(s1.check_invariants())

    def test_batch_restore_subtree_matches_sequential(self):
        s1 = build_trash_fixture()
        s1.restore_many(["a", "d"], subtree=True)
        s2 = build_trash_fixture()
        s2.restore("a", subtree=True)
        s2.restore("d", subtree=True)
        self.assertEqual(s1.snapshot(), s2.snapshot())

    def test_batch_restore_partial_failure_details_and_rollback(self):
        """中途失败：保留中间状态、给出失败明细，可回滚到批量前。"""
        s = build_trash_fixture()
        s.purge("d1")  # 让 d1 彻底不存在，恢复它必然失败
        before = s.snapshot()
        result = s.restore_many(["a1x", "a1", "d1", "a2"])
        self.assertFalse(result.ok)
        self.assertEqual(result.succeeded, ["a1x", "a1"])  # 中间状态保留
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0]["id"], "d1")
        self.assertEqual(result.failed[0]["error"], "NotFoundError")
        self.assertIn("d1", result.failed[0]["message"])
        self.assertEqual(result.pending, ["a2"])
        # 中间状态：a1x/a1 已恢复，a2 仍在回收站
        self.assertTrue(s.exists("a1"))
        self.assertTrue(s.in_trash("a2"))
        # 回滚后与批量操作前完全一致
        result.rollback()
        self.assertTrue(result.rolled_back)
        self.assertEqual(s.snapshot(), before)
        self.assertTrue(s.in_trash("a1x"))
        self.assertTrue(s.check_invariants())
        with self.assertRaises(StoreError):
            result.rollback()  # 不能重复回滚

    def test_batch_restore_atomic_auto_rollback(self):
        s = build_trash_fixture()
        before = s.snapshot()
        result = s.restore_many(["a1x", "nope", "a1"], atomic=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.rolled_back)
        self.assertEqual(result.pending, ["a1"])
        self.assertEqual(s.snapshot(), before)  # 已自动回滚

    def test_batch_restore_continue_on_error(self):
        s = build_trash_fixture()
        result = s.restore_many(["a1x", "nope", "a1"],
                                continue_on_error=True)
        self.assertEqual(result.succeeded, ["a1x", "a1"])
        self.assertEqual([f["id"] for f in result.failed], ["nope"])
        self.assertEqual(result.pending, [])

    def test_batch_restore_reject_policy_atomic_per_item(self):
        """单项 REJECT 失败不污染状态，批量结果记录该失败。"""
        s = TreeStore()
        s.add("a")
        s.add("b", "a")
        s.delete("a", cascade=True)
        before = s.snapshot()
        result = s.restore_many(["b"], policy=REJECT)
        self.assertFalse(result.ok)
        self.assertEqual(result.failed[0]["error"], "ParentMissingError")
        self.assertEqual(s.snapshot(), before)


class TestBatchPurge(unittest.TestCase):
    def test_batch_purge_matches_sequential(self):
        s1 = build_trash_fixture()
        result = s1.purge_many(["c1", "b", "a"])  # a 级联清除整棵
        self.assertTrue(result.ok)
        s2 = build_trash_fixture()
        for nid in ("c1", "b", "a"):
            s2.purge(nid)
        self.assertEqual(s1.snapshot(), s2.snapshot())
        self.assertEqual([t["id"] for t in s1.trash()], ["d", "d1"])
        self.assertTrue(s1.check_invariants())

    def test_batch_purge_failure_details_and_rollback(self):
        """purge 是物理删除，但批量回滚基于检查点，可完整还原。"""
        s = build_trash_fixture()
        before = s.snapshot()
        result = s.purge_many(["c1", "ghost", "b"])
        self.assertFalse(result.ok)
        self.assertEqual(result.succeeded, ["c1"])
        self.assertEqual(result.failed[0]["id"], "ghost")
        self.assertEqual(result.failed[0]["error"], "NotFoundError")
        self.assertEqual(result.pending, ["b"])
        self.assertFalse(s.in_trash("c1"))  # 中间状态：c1 已被清除
        result.rollback()
        self.assertEqual(s.snapshot(), before)  # c1 完整还原
        self.assertTrue(s.in_trash("c1"))
        self.assertTrue(s.check_invariants())

    def test_batch_purge_atomic(self):
        s = build_trash_fixture()
        before = s.snapshot()
        result = s.purge_many(["c1", "b", "ghost"], atomic=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.rolled_back)
        self.assertEqual(s.snapshot(), before)

    def test_batch_purge_live_node_rejected(self):
        s = build_trash_fixture()
        result = s.purge_many(["c"])  # 活节点不在回收站
        self.assertFalse(result.ok)
        self.assertEqual(result.failed[0]["error"], "NotInTrashError")


if __name__ == "__main__":
    unittest.main(verbosity=2)
