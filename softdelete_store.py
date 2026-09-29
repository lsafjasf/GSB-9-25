"""软删除与回收站管理库（仅依赖 Python 标准库）。

核心语义
--------
- 普通查询（get / children / find / roots / list_live）永远只返回未删除节点；
- delete 软删除：节点保留在库中，标记 deleted，并记录原父节点与完整祖先链，
  供回收站视图与"最近存在祖先"恢复策略使用；
- restore 恢复：子树结构、父子关系、按属性索引全部还原，不留删除痕迹；
- purge 彻底清除：物理移除，不可恢复；
- 原父节点不存在时的三种恢复策略：
    ROOT             恢复到根（parent=None）
    REJECT           拒绝并抛出 ParentMissingError
    NEAREST_ANCESTOR 恢复到最近存在的祖先，没有则到根
- 级联删除是原子的：中途失败（可通过 hook 注入）会完整回滚，状态与删除前一致。

确定性约定
----------
- 重复删除            -> AlreadyDeletedError
- 删除/恢复不存在节点  -> NotFoundError（含 purge 后再 restore）
- 恢复未删除节点       -> NotDeletedError
- 非级联删除有活子节点 -> HasLiveChildrenError
- purge 未删除节点     -> NotInTrashError
- purge(cascade=False) 只清除自身，其子节点重新挂到根；已删除子节点的
  原父信息仍保留，恢复时按上述三种策略处理。

回收站增强
----------
- 每个已删除节点记录删除元数据：deleted_at（删除时间，时钟可注入）、
  deleted_seq（全局单调删除序号，保证分页有稳定全序）、
  deleted_root（本次删除操作的目标节点，即级联根）；
- trash_query(...) 支持按原父节点、删除时间区间、是否/哪个级联根筛选，
  以及 offset/limit 分页；同一筛选条件下各页拼接与全量列表一致；
- trash_children / trash_tree 支持按层级展开已删除子树；
- restore_many / purge_many 批量操作：中途失败时返回 BatchResult，
  含成功/失败明细与未处理列表，并可通过 result.rollback() 把存储
  回滚到批量操作开始前的状态（atomic=True 时自动回滚）。
"""

from __future__ import annotations

import time

# 恢复策略常量
ROOT = "root"
REJECT = "reject"
NEAREST_ANCESTOR = "nearest_ancestor"
_POLICIES = (ROOT, REJECT, NEAREST_ANCESTOR)


class StoreError(Exception):
    """所有库异常的基类。"""


class NotFoundError(StoreError):
    """节点不存在（从未加入，或已被彻底清除）。"""


class AlreadyDeletedError(StoreError):
    """重复删除。"""


class NotDeletedError(StoreError):
    """恢复一个未删除的节点。"""


class HasLiveChildrenError(StoreError):
    """非级联删除仍有未删除子节点的节点。"""


class NotInTrashError(StoreError):
    """purge 一个未删除（不在回收站）的节点。"""


class ParentMissingError(StoreError):
    """REJECT 策略下原父节点已不存在。"""


class DeleteFailedError(StoreError):
    """级联删除中途失败；状态已完整回滚。"""


class _Node:
    __slots__ = ("id", "parent_id", "attrs", "deleted",
                 "deleted_parent", "deleted_ancestors",
                 "deleted_at", "deleted_seq", "deleted_root")

    def __init__(self, node_id, parent_id, attrs):
        self.id = node_id
        self.parent_id = parent_id          # 当前父节点（软删除不改变它）
        self.attrs = dict(attrs)            # 属性值须可哈希，才能进入索引
        self.deleted = False
        self.deleted_parent = None          # 删除时的原父节点（回收站视图用）
        self.deleted_ancestors = None       # 删除时的祖先链（父->...->根）
        self.deleted_at = None              # 删除时间戳（时钟可注入）
        self.deleted_seq = None             # 全局单调删除序号（分页稳定序）
        self.deleted_root = None            # 本次删除操作的目标节点（级联根）


# trash_query 的 original_parent 缺省哨兵（None 本身是合法筛选值：根层删除）
_ANY = object()


class BatchResult:
    """批量操作结果：成功/失败明细 + 可回滚的中间状态。

    - succeeded: 已成功处理的节点 id（按处理顺序）
    - failed:    失败明细 [{"id", "error", "message"}]
    - pending:   因中途失败而未处理的节点 id
    - rollback(): 把存储回滚到批量操作开始前的状态，只能调用一次
    """

    def __init__(self, store, checkpoint, op):
        self.op = op
        self.succeeded = []
        self.failed = []
        self.pending = []
        self.rolled_back = False
        self._store = store
        self._checkpoint = checkpoint

    @property
    def ok(self):
        return not self.failed

    def rollback(self):
        """回滚到批量操作开始前的状态（含已被 purge 的节点）。"""
        if self._checkpoint is None:
            raise StoreError("该批量结果已回滚，不能重复回滚")
        self._store._restore_checkpoint(self._checkpoint)
        self._checkpoint = None
        self.rolled_back = True
        return True

    def summary(self):
        return ("%s: ok=%d failed=%d pending=%d rolled_back=%s"
                % (self.op, len(self.succeeded), len(self.failed),
                   len(self.pending), self.rolled_back))


class TreeStore:
    """带软删除、回收站与按属性索引的树形存储。"""

    def __init__(self, clock=None):
        self._nodes = {}      # id -> _Node（含已删除节点）
        self._children = {}   # parent_id(None 表示根) -> set(child_id)，含已删除节点
        self._index = {}      # attr -> value -> set(live_id)，只索引未删除节点
        self._clock = clock or time.time  # 删除时间戳来源，测试可注入假时钟
        self._seq = 0         # 全局删除序号，保证回收站分页有稳定全序

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def add(self, node_id, parent_id=None, **attrs):
        """新增节点。父节点必须存在且未删除。"""
        if node_id in self._nodes:
            raise StoreError("节点已存在: %r" % (node_id,))
        if parent_id is not None:
            parent = self._nodes.get(parent_id)
            if parent is None:
                raise NotFoundError("父节点不存在: %r" % (parent_id,))
            if parent.deleted:
                raise StoreError("父节点已删除，不能挂载: %r" % (parent_id,))
        node = _Node(node_id, parent_id, attrs)
        self._nodes[node_id] = node
        self._children.setdefault(parent_id, set()).add(node_id)
        self._index_add(node)
        return node_id

    def set_attr(self, node_id, name, value):
        """设置/更新属性并维护索引。仅允许未删除节点。"""
        node = self._require_live(node_id)
        self._index_remove(node)
        node.attrs[name] = value
        self._index_add(node)

    def delete(self, node_id, cascade=False, hook=None):
        """软删除。cascade=True 时级联删除整棵子树，且是原子的：
        中途任何异常（可用 hook 注入）都会完整回滚并抛出 DeleteFailedError。
        返回本次删除的节点数。
        """
        node = self._nodes.get(node_id)
        if node is None:
            raise NotFoundError("节点不存在: %r" % (node_id,))
        if node.deleted:
            raise AlreadyDeletedError("节点已在回收站: %r" % (node_id,))
        if cascade:
            targets = [t for t in self._subtree_ids(node_id)
                       if not self._nodes[t].deleted]
        else:
            live_children = [c for c in self._children.get(node_id, ())
                             if not self._nodes[c].deleted]
            if live_children:
                raise HasLiveChildrenError(
                    "节点 %r 仍有未删除子节点，需 cascade=True" % (node_id,))
            targets = [node_id]

        done = []
        try:
            for tid in targets:
                target = self._nodes[tid]
                self._apply_delete(target, root_id=node_id)
                done.append(target)
                if hook is not None:
                    hook(tid)
        except Exception as exc:
            for target in reversed(done):
                self._undo_delete(target)
            raise DeleteFailedError(
                "级联删除 %r 失败，已回滚 %d 个节点: %s"
                % (node_id, len(done), exc)) from exc
        return len(done)

    def restore(self, node_id, policy=NEAREST_ANCESTOR, subtree=False):
        """恢复节点；subtree=True 时连同其已删除后代一起恢复（父先子后）。
        原父节点不存在时按 policy 处理。恢复是两阶段的：先解析全部目标父节点
        （REJECT 失败时不产生任何修改），再统一应用。返回恢复节点数。
        """
        if policy not in _POLICIES:
            raise ValueError("未知恢复策略: %r" % (policy,))
        node = self._nodes.get(node_id)
        if node is None:
            raise NotFoundError("节点不存在或已被彻底清除: %r" % (node_id,))
        if not node.deleted:
            raise NotDeletedError("节点未被删除: %r" % (node_id,))

        if subtree:
            targets = [t for t in self._subtree_ids(node_id)
                       if self._nodes[t].deleted]  # 先序遍历 => 父先于子
        else:
            targets = [node_id]

        # 阶段一：解析所有目标父节点（此时不做任何修改）。
        # 同批恢复的目标节点视为"即将存在"，否则子树内节点会误判父节点缺失。
        batch = set(targets)
        plan = [(tid, self._resolve_parent(self._nodes[tid], policy, batch))
                for tid in targets]
        # 阶段二：应用
        for tid, new_parent in plan:
            self._apply_restore(self._nodes[tid], new_parent)
        return len(plan)

    def purge(self, node_id, cascade=True):
        """彻底清除回收站中的节点。cascade=True（默认）连同其已删除后代一起清除；
        cascade=False 只清除自身，其子节点（无论是否已删除）重新挂到根。
        返回清除节点数。
        """
        node = self._nodes.get(node_id)
        if node is None:
            raise NotFoundError("节点不存在: %r" % (node_id,))
        if not node.deleted:
            raise NotInTrashError("节点未删除，不能清除: %r" % (node_id,))
        if cascade:
            targets = [t for t in self._subtree_ids(node_id)
                       if self._nodes[t].deleted]
        else:
            targets = [node_id]
        for tid in targets:
            self._purge_one(self._nodes[tid])
        return len(targets)

    def purge_all(self):
        """清空回收站。返回清除节点数。"""
        targets = [nid for nid, n in self._nodes.items() if n.deleted]
        for nid in targets:
            self._purge_one(self._nodes[nid])
        return len(targets)

    # ------------------------------------------------------------------ #
    # 批量操作（失败可回滚）
    # ------------------------------------------------------------------ #
    def restore_many(self, ids, policy=NEAREST_ANCESTOR, subtree=False,
                     atomic=False, continue_on_error=False):
        """按给定顺序逐个恢复。语义与手动逐个调用 restore 完全一致。

        中途失败：默认停止并返回 BatchResult（含失败明细与 pending 列表），
        已成功的部分保留为中间状态，可调用 result.rollback() 整体回滚；
        atomic=True 时失败即自动回滚；continue_on_error=True 时跳过失败继续。
        """
        checkpoint = self._checkpoint()
        result = BatchResult(self, checkpoint, "restore_many")
        ids = list(ids)
        for pos, nid in enumerate(ids):
            try:
                self.restore(nid, policy=policy, subtree=subtree)
                result.succeeded.append(nid)
            except StoreError as exc:
                result.failed.append({
                    "id": nid, "error": type(exc).__name__,
                    "message": str(exc)})
                if atomic:
                    result.pending = ids[pos + 1:]
                    result.rollback()
                    break
                if not continue_on_error:
                    result.pending = ids[pos + 1:]
                    break
        return result

    def purge_many(self, ids, cascade=True,
                   atomic=False, continue_on_error=False):
        """按给定顺序逐个彻底清除。失败处理与 restore_many 相同；
        回滚基于操作前检查点，因此被 purge 的节点也能完整还原。"""
        checkpoint = self._checkpoint()
        result = BatchResult(self, checkpoint, "purge_many")
        ids = list(ids)
        for pos, nid in enumerate(ids):
            try:
                self.purge(nid, cascade=cascade)
                result.succeeded.append(nid)
            except StoreError as exc:
                result.failed.append({
                    "id": nid, "error": type(exc).__name__,
                    "message": str(exc)})
                if atomic:
                    result.pending = ids[pos + 1:]
                    result.rollback()
                    break
                if not continue_on_error:
                    result.pending = ids[pos + 1:]
                    break
        return result

    # ------------------------------------------------------------------ #
    # 普通查询（只见未删除项）
    # ------------------------------------------------------------------ #
    def exists(self, node_id):
        node = self._nodes.get(node_id)
        return node is not None and not node.deleted

    def get(self, node_id):
        """返回属性字典副本；节点不存在或已删除都抛 NotFoundError。"""
        return dict(self._require_live(node_id).attrs)

    def parent(self, node_id):
        return self._require_live(node_id).parent_id

    def children(self, node_id=None):
        """未删除子节点列表（node_id=None 表示根层）。"""
        if node_id is not None:
            self._require_live(node_id)
        return sorted((c for c in self._children.get(node_id, ())
                       if not self._nodes[c].deleted), key=repr)

    def roots(self):
        return self.children(None)

    def list_live(self):
        return sorted((nid for nid, n in self._nodes.items() if not n.deleted),
                      key=repr)

    def find(self, attr, value):
        """按属性索引查询，只返回未删除节点。"""
        return sorted(self._index.get(attr, {}).get(value, ()), key=repr)

    # ------------------------------------------------------------------ #
    # 回收站视图
    # ------------------------------------------------------------------ #
    def trash(self):
        """全部已删除项：[{"id", "original_parent", "attrs"}]，按 id 排序。"""
        return [self._trash_item(n)
                for n in sorted(self._nodes.values(), key=lambda n: repr(n.id))
                if n.deleted]

    def trash_roots(self):
        """回收站顶层项：已删除且其父节点未删除的节点 id。"""
        return sorted((n.id for n in self._nodes.values()
                       if n.deleted and (n.parent_id is None
                                         or not self._nodes[n.parent_id].deleted)),
                      key=repr)

    def in_trash(self, node_id):
        node = self._nodes.get(node_id)
        return node is not None and node.deleted

    # ------------------------------------------------------------------ #
    # 回收站：筛选 + 分页 + 层级展开
    # ------------------------------------------------------------------ #
    def trash_query(self, original_parent=_ANY, deleted_after=None,
                    deleted_before=None, cascade_root=None,
                    offset=0, limit=None):
        """筛选 + 分页查询回收站。

        筛选条件（可组合）：
        - original_parent: 原父节点 id；None 表示只查根层删除，缺省不过滤
        - deleted_after / deleted_before: 删除时间区间，两端闭区间
        - cascade_root: True 只看删除操作的目标节点（级联根），
          False 只看被级联带出的节点，传入具体 id 则只看该次删除操作的节点，
          None 不过滤
        分页：offset / limit；结果按 deleted_seq（删除先后顺序）稳定排序，
        因此同一筛选条件下各页按序拼接与全量列表完全一致。
        返回 {"total", "offset", "limit", "items"}。
        """
        if offset < 0:
            raise ValueError("offset 不能为负: %r" % (offset,))
        if limit is not None and limit < 0:
            raise ValueError("limit 不能为负: %r" % (limit,))
        items = []
        for node in self._nodes.values():
            if not node.deleted:
                continue
            if original_parent is not _ANY \
                    and node.deleted_parent != original_parent:
                continue
            if deleted_after is not None and node.deleted_at < deleted_after:
                continue
            if deleted_before is not None and node.deleted_at > deleted_before:
                continue
            if cascade_root is True and node.deleted_root != node.id:
                continue
            if cascade_root is False and node.deleted_root == node.id:
                continue
            if cascade_root not in (None, True, False) \
                    and node.deleted_root != cascade_root:
                continue
            items.append(self._trash_item(node))
        items.sort(key=lambda item: item["deleted_seq"])
        total = len(items)
        end = None if limit is None else offset + limit
        return {"total": total, "offset": offset, "limit": limit,
                "items": items[offset:end]}

    def trash_iter_pages(self, page_size, **filters):
        """按页迭代 trash_query 结果，供"分页拉取再拼接"的调用方使用。"""
        if page_size <= 0:
            raise ValueError("page_size 必须为正: %r" % (page_size,))
        offset = 0
        while True:
            page = self.trash_query(offset=offset, limit=page_size, **filters)
            if not page["items"]:
                break
            yield page
            offset += page_size

    def trash_children(self, node_id=None):
        """按层级展开：返回某已删除节点的已删除子节点（回收站条目格式）。
        node_id=None 时返回回收站顶层项（等价于 trash_roots 的条目形式）。
        """
        if node_id is None:
            ids = self.trash_roots()
        else:
            node = self._nodes.get(node_id)
            if node is None:
                raise NotFoundError("节点不存在: %r" % (node_id,))
            if not node.deleted:
                raise NotInTrashError("节点未删除，不在回收站: %r" % (node_id,))
            ids = [c for c in self._children.get(node_id, ())
                   if self._nodes[c].deleted]
        items = [self._trash_item(self._nodes[i]) for i in ids]
        items.sort(key=lambda item: item["deleted_seq"])
        return items

    def trash_tree(self, root_id=None):
        """整棵展开已删除子树（迭代实现，深树不溢出）。
        root_id=None 时以回收站全部顶层项为根展开成森林。
        返回 [{"id", "original_parent", "deleted_at", "cascade_root",
               "attrs", "children": [...]}]。
        """
        if root_id is None:
            roots = self.trash_roots()
        else:
            node = self._nodes.get(root_id)
            if node is None:
                raise NotFoundError("节点不存在: %r" % (root_id,))
            if not node.deleted:
                raise NotInTrashError("节点未删除，不在回收站: %r" % (root_id,))
            roots = [root_id]
        return [self._build_trash_tree(rid) for rid in roots]

    def _build_trash_tree(self, root_id):
        root_item = self._tree_item(root_id)
        stack = [root_item]
        while stack:
            item = stack.pop()
            deleted_kids = sorted(
                (c for c in self._children.get(item["id"], ())
                 if self._nodes[c].deleted), key=repr)
            for child_id in deleted_kids:
                child_item = self._tree_item(child_id)
                item["children"].append(child_item)
                stack.append(child_item)
        return root_item

    # ------------------------------------------------------------------ #
    # 一致性断言 / 快照
    # ------------------------------------------------------------------ #
    def snapshot(self):
        """全量规范化快照（节点父子关系、删除标记、属性、索引），
        可直接用 == 对比"恢复后是否与删除前一致"。"""
        return {
            "nodes": {nid: (n.parent_id, n.deleted, frozenset(n.attrs.items()),
                            n.deleted_parent, n.deleted_at, n.deleted_seq,
                            n.deleted_root)
                      for nid, n in self._nodes.items()},
            "index": {a: {v: frozenset(ids) for v, ids in vals.items()}
                      for a, vals in self._index.items()},
        }

    def check_invariants(self):
        """内部不变量自检：父子结构、children 表、属性索引三者必须互相一致。
        任何不一致都会抛 AssertionError。"""
        for nid, node in self._nodes.items():
            assert node.parent_id is None or node.parent_id in self._nodes, \
                "悬空父引用: %r" % (nid,)
            if node.deleted:
                assert node.deleted_ancestors is not None, \
                    "已删除节点缺少祖先链: %r" % (nid,)
                assert node.deleted_at is not None \
                    and node.deleted_seq is not None \
                    and node.deleted_root is not None, \
                    "已删除节点缺少删除元数据: %r" % (nid,)
            else:
                assert node.deleted_parent is None, \
                    "活节点残留删除痕迹: %r" % (nid,)
                assert node.deleted_at is None and node.deleted_seq is None \
                    and node.deleted_root is None, \
                    "活节点残留删除元数据: %r" % (nid,)
        seqs = [n.deleted_seq for n in self._nodes.values() if n.deleted]
        assert len(seqs) == len(set(seqs)), "删除序号不唯一"
        # children 表与 parent_id 完全一致
        rebuilt = {}
        for nid, node in self._nodes.items():
            rebuilt.setdefault(node.parent_id, set()).add(nid)
        assert rebuilt == {k: v for k, v in self._children.items() if v}, \
            "children 表与父子关系不一致"
        # 索引与活节点属性完全一致
        rebuilt_index = {}
        for nid, node in self._nodes.items():
            if node.deleted:
                continue
            for key, value in node.attrs.items():
                rebuilt_index.setdefault(key, {}).setdefault(value, set()).add(nid)
        assert rebuilt_index == self._index, "属性索引与节点属性不一致"
        return True

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _require_live(self, node_id):
        node = self._nodes.get(node_id)
        if node is None or node.deleted:
            raise NotFoundError("节点不存在或已删除: %r" % (node_id,))
        return node

    def _subtree_ids(self, node_id):
        """迭代先序遍历（父先于子，子节点按 id 排序），避免深树递归溢出；
        顺序确定，保证删除序号与回收站分页序可复现。"""
        order = []
        stack = [node_id]
        while stack:
            current = stack.pop()
            order.append(current)
            stack.extend(sorted(self._children.get(current, ()),
                                key=repr, reverse=True))
        return order

    def _ancestor_chain(self, parent_id):
        chain = []
        current = parent_id
        while current is not None and current in self._nodes:
            chain.append(current)
            current = self._nodes[current].parent_id
        return tuple(chain)

    def _apply_delete(self, node, root_id):
        node.deleted = True
        node.deleted_parent = node.parent_id
        node.deleted_ancestors = self._ancestor_chain(node.parent_id)
        node.deleted_at = self._clock()
        node.deleted_seq = self._seq
        node.deleted_root = root_id
        self._seq += 1
        self._index_remove(node)

    def _undo_delete(self, node):
        node.deleted = False
        node.deleted_parent = None
        node.deleted_ancestors = None
        node.deleted_at = None
        node.deleted_seq = None
        node.deleted_root = None
        self._index_add(node)

    def _resolve_parent(self, node, policy, batch=frozenset()):
        original = node.deleted_parent
        if original is None:
            return None
        parent = self._nodes.get(original)
        if parent is not None and (not parent.deleted or original in batch):
            return original
        if policy == REJECT:
            raise ParentMissingError(
                "原父节点 %r 已不存在，拒绝恢复 %r" % (original, node.id))
        if policy == ROOT:
            return None
        for ancestor in node.deleted_ancestors:
            candidate = self._nodes.get(ancestor)
            if candidate is not None and (not candidate.deleted
                                          or ancestor in batch):
                return ancestor
        return None

    def _apply_restore(self, node, new_parent):
        old_parent = node.parent_id
        if old_parent != new_parent:
            old_set = self._children.get(old_parent)
            if old_set is not None:
                old_set.discard(node.id)
                if not old_set:
                    del self._children[old_parent]
            self._children.setdefault(new_parent, set()).add(node.id)
            node.parent_id = new_parent
        node.deleted = False
        node.deleted_parent = None
        node.deleted_ancestors = None
        node.deleted_at = None
        node.deleted_seq = None
        node.deleted_root = None
        self._index_add(node)

    # ------------------------------------------------------------------ #
    # 检查点 / 回滚（批量操作与级联删除失败回滚的基础）
    # ------------------------------------------------------------------ #
    def _checkpoint(self):
        """全量状态检查点：节点字段、children 表、索引、删除序号。"""
        nodes_state = {
            nid: (n.parent_id, dict(n.attrs), n.deleted, n.deleted_parent,
                  n.deleted_ancestors, n.deleted_at, n.deleted_seq,
                  n.deleted_root)
            for nid, n in self._nodes.items()}
        children = {k: set(v) for k, v in self._children.items()}
        index = {a: {v: set(ids) for v, ids in vals.items()}
                 for a, vals in self._index.items()}
        return (nodes_state, children, index, self._seq)

    def _restore_checkpoint(self, checkpoint):
        """回滚到检查点：被 purge 的节点完整还原，多余的节点移除。"""
        nodes_state, children, index, seq = checkpoint
        for nid in list(self._nodes):
            if nid not in nodes_state:
                del self._nodes[nid]
        for nid, state in nodes_state.items():
            node = self._nodes.get(nid)
            if node is None:
                node = _Node(nid, state[0], state[1])
                self._nodes[nid] = node
            (node.parent_id, node.attrs, node.deleted, node.deleted_parent,
             node.deleted_ancestors, node.deleted_at, node.deleted_seq,
             node.deleted_root) = (
                state[0], dict(state[1]), state[2], state[3],
                state[4], state[5], state[6], state[7])
        self._children = {k: set(v) for k, v in children.items()}
        self._index = {a: {v: set(ids) for v, ids in vals.items()}
                       for a, vals in index.items()}
        self._seq = seq

    def _trash_item(self, node):
        return {"id": node.id,
                "original_parent": node.deleted_parent,
                "deleted_at": node.deleted_at,
                "deleted_seq": node.deleted_seq,
                "cascade_root": node.deleted_root,
                "is_cascade_root": node.deleted_root == node.id,
                "attrs": dict(node.attrs)}

    def _tree_item(self, node_id):
        node = self._nodes[node_id]
        return {"id": node.id,
                "original_parent": node.deleted_parent,
                "deleted_at": node.deleted_at,
                "cascade_root": node.deleted_root,
                "attrs": dict(node.attrs),
                "children": []}

    def _purge_one(self, node):
        siblings = self._children.get(node.parent_id)
        if siblings is not None:
            siblings.discard(node.id)
            if not siblings:
                del self._children[node.parent_id]
        # 子节点（若有）重新挂到根，避免悬空父引用；
        # 已删除子节点的原父信息仍保留在 deleted_parent/deleted_ancestors 中。
        orphans = self._children.pop(node.id, ())
        for child_id in orphans:
            self._nodes[child_id].parent_id = None
        if orphans:
            self._children.setdefault(None, set()).update(orphans)
        del self._nodes[node.id]

    def _index_add(self, node):
        for key, value in node.attrs.items():
            self._index.setdefault(key, {}).setdefault(value, set()).add(node.id)

    def _index_remove(self, node):
        for key, value in node.attrs.items():
            values = self._index.get(key)
            if values is None:
                continue
            ids = values.get(value)
            if ids is not None:
                ids.discard(node.id)
                if not ids:
                    del values[value]
            if not values:
                del self._index[key]
