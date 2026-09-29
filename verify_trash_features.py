#!/usr/bin/env python3
"""回收站功能迭代的可复跑验证脚本（真实输出）。

覆盖：
  1. 分页：各页拼接与全量列表一致（含筛选条件）
  2. 筛选：原父节点 / 删除时间区间 / 是否级联根
  3. 层级展开已删除子树
  4. 批量恢复：与逐个恢复结构一致；中途失败的中间状态、失败明细与回滚
  5. 批量彻底清除：中途失败回滚（含已 purge 节点的还原）

运行：python3 verify_trash_features.py   （退出码 0 = 全部通过）
"""

from softdelete_store import TreeStore


class FakeClock:
    def __init__(self, start=0):
        self.now = start

    def __call__(self):
        return self.now


def build_store():
    """t=100 级联删 a 子树；t=200 删 b；t=300 删 c1；t=400 级联删 d 子树。"""
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


def show(title):
    print("\n== %s ==" % title)


def main():
    passed = 0

    show("1. 分页：page_size=3 逐页拉取，拼接与全量一致")
    s = build_store()
    full = s.trash_query()["items"]
    print("全量 %d 条（按删除先后）: %s"
          % (len(full), [i["id"] for i in full]))
    concat = []
    for page in s.trash_iter_pages(3):
        ids = [i["id"] for i in page["items"]]
        print("  offset=%d -> %s" % (page["offset"], ids))
        concat.extend(page["items"])
    assert concat == full, "分页拼接与全量不一致"
    for size in range(1, 12):  # 所有页大小均一致
        items = []
        for page in s.trash_iter_pages(size):
            items.extend(page["items"])
        assert items == full, "page_size=%d 不一致" % size
    print("PASS: page_size=1..11 拼接均与全量一致")
    passed += 1

    show("2. 筛选：原父节点 / 删除时间区间 / 是否级联根")
    q1 = s.trash_query(original_parent="a")
    print("original_parent='a' ->", [i["id"] for i in q1["items"]])
    assert {i["id"] for i in q1["items"]} == {"a1", "a2"}
    q2 = s.trash_query(deleted_after=200, deleted_before=300)
    print("deleted_at in [200,300] ->", [i["id"] for i in q2["items"]])
    assert {i["id"] for i in q2["items"]} == {"b", "c1"}
    q3 = s.trash_query(cascade_root=True)
    q4 = s.trash_query(cascade_root="a")
    print("cascade_root=True     ->", [i["id"] for i in q3["items"]])
    print("cascade_root='a'      ->", [i["id"] for i in q4["items"]])
    assert {i["id"] for i in q3["items"]} == {"a", "b", "c1", "d"}
    assert {i["id"] for i in q4["items"]} == {"a", "a1", "a2", "a1x"}
    # 筛选 + 分页组合一致性
    filtered = s.trash_query(original_parent=None, deleted_after=100)["items"]
    got = []
    for page in s.trash_iter_pages(2, original_parent=None, deleted_after=100):
        got.extend(page["items"])
    assert got == filtered
    print("PASS: 三类筛选正确，筛选+分页组合拼接一致")
    passed += 1

    show("3. 层级展开已删除子树")
    def render(items, indent=2):
        for it in items:
            print(" " * indent + "%s (deleted_at=%s, cascade_root=%s)"
                  % (it["id"], it["deleted_at"], it["cascade_root"]))
            render(it.get("children", []), indent + 2)
    render(s.trash_tree())
    level1 = [i["id"] for i in s.trash_children()]
    level2 = [i["id"] for i in s.trash_children("a")]
    level3 = [i["id"] for i in s.trash_children("a1")]
    print("逐层展开: 顶层=%s, a 下=%s, a1 下=%s" % (level1, level2, level3))
    assert level1 == ["a", "b", "c1", "d"]
    assert level2 == ["a1", "a2"] and level3 == ["a1x"]
    print("PASS: 整树展开与逐层展开一致")
    passed += 1

    show("4. 批量恢复 == 逐个恢复；中途失败可回滚")
    ids = ["a1x", "a1", "a2", "a", "c1", "d1", "d", "b"]
    s1, s2 = build_store(), build_store()
    r = s1.restore_many(ids)
    for nid in ids:
        s2.restore(nid)
    assert s1.snapshot() == s2.snapshot()
    print("restore_many(%s) -> %s" % (ids, r.summary()))
    print("PASS: 批量恢复快照与逐个恢复完全一致")

    s3 = build_store()
    s3.purge("d1")  # 制造必然失败的中间项
    before = s3.snapshot()
    r3 = s3.restore_many(["a1x", "a1", "d1", "a2"])
    print("中途失败: %s" % r3.summary())
    print("  失败明细: %s" % r3.failed)
    print("  中间状态: a1 已恢复=%s, a2 仍在回收站=%s"
          % (s3.exists("a1"), s3.in_trash("a2")))
    assert r3.succeeded == ["a1x", "a1"] and r3.pending == ["a2"]
    r3.rollback()
    assert s3.snapshot() == before
    print("  rollback() 后: 快照与批量前一致, a1x 回到回收站=%s"
          % s3.in_trash("a1x"))
    print("PASS: 失败明细完整，中间状态可回滚")
    passed += 1

    show("5. 批量彻底清除：中途失败回滚（purge 也可还原）")
    s4 = build_store()
    before = s4.snapshot()
    r4 = s4.purge_many(["c1", "ghost", "b"])
    print("中途失败: %s" % r4.summary())
    print("  失败明细: %s" % r4.failed)
    assert r4.succeeded == ["c1"] and not s4.in_trash("c1")
    r4.rollback()
    assert s4.snapshot() == before and s4.in_trash("c1")
    print("  rollback() 后: 已 purge 的 c1 完整还原=%s" % s4.in_trash("c1"))
    r5 = s4.purge_many(["c1", "b", "a"], atomic=True)
    print("atomic 全成功: %s, 回收站剩余=%s"
          % (r5.summary(), [t["id"] for t in s4.trash()]))
    assert r5.ok and [t["id"] for t in s4.trash()] == ["d", "d1"]
    print("PASS: purge 失败可回滚，atomic 批量清除正确")
    passed += 1

    print("\n全部 %d 项验证通过。" % passed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
