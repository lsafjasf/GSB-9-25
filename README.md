# 软删除与回收站管理库（Python 3，仅标准库）

## 文件

- `softdelete_store.py` — 库源码：`TreeStore` 树形存储，支持软删除、级联删除、
  恢复（单节点/整棵子树）、彻底清除、回收站视图、按属性索引。
- `test_softdelete_store.py` — 自测集（53 个用例，unittest）。
- `verify_trash_features.py` — 回收站迭代的可复跑验证脚本（真实输出）：
  分页拼接一致性、三类筛选、层级展开、批量恢复/清除的失败回滚。

## 运行

```bash
python3 -m unittest test_softdelete_store -v
# 或
python3 test_softdelete_store.py
python3 verify_trash_features.py
```

## 核心语义

- 普通查询（`get` / `children` / `find` / `roots` / `list_live`）只返回未删除节点；
  回收站视图 `trash()` 返回全部已删除项及其原父节点，`trash_roots()` 返回顶层项。
- `delete(id)` 软删除叶子；`delete(id, cascade=True)` 级联删除整棵子树，且**原子**：
  中途失败（可用 `hook` 注入故障）完整回滚并抛 `DeleteFailedError`。
- `restore(id, policy=..., subtree=...)` 恢复单节点或整棵子树；恢复是两阶段的，
  `REJECT` 失败时不产生任何修改。
- `purge(id)` / `purge_all()` 彻底清除，不可恢复。

## 回收站：分页、筛选与层级展开

- 每个已删除节点记录 `deleted_at`（删除时间，时钟可注入 `TreeStore(clock=...)`）、
  `deleted_seq`（全局单调删除序号）、`deleted_root`（本次删除操作的目标节点）。
- `trash_query(original_parent=..., deleted_after=..., deleted_before=...,
  cascade_root=..., offset=..., limit=...)`：三类筛选可组合，分页按
  `deleted_seq` 稳定排序，**同一筛选条件下各页拼接与全量列表一致**；
  `trash_iter_pages(page_size, **filters)` 逐页迭代。
- `cascade_root`：`True` 只看删除操作的目标节点（级联根），`False` 只看被
  级联带出的节点，传具体 id 只看该次删除操作删除的节点。
- `trash_children(node_id=None)` 逐层展开；`trash_tree(root_id=None)` 整棵
  展开已删除子树（迭代实现，2000 层深链不溢出）。

## 批量恢复 / 批量彻底清除

- `restore_many(ids, policy=..., subtree=..., atomic=..., continue_on_error=...)`
  按给定顺序逐个恢复，**最终结构与手动逐个 restore 完全一致**；
  `purge_many(ids, cascade=..., ...)` 同理。
- 中途失败返回 `BatchResult`：`succeeded`（已处理）、`failed`（失败明细：
  id + 异常类型 + 消息）、`pending`（未处理）；中间状态保留，调用
  `result.rollback()` 可整体回滚到批量开始前（基于检查点，已被 purge 的
  节点也能完整还原）；`atomic=True` 时失败即自动回滚。

## 原父节点不存在时的三种恢复策略

| 策略 | 常量 | 行为 |
|---|---|---|
| 恢复到根 | `ROOT` | 挂到根层（parent=None） |
| 拒绝 | `REJECT` | 抛 `ParentMissingError`，状态不变 |
| 最近存在祖先 | `NEAREST_ANCESTOR`（默认） | 沿删除时记录的祖先链向上找最近存活祖先，没有则到根 |

## 恢复一致性断言

- `snapshot()` 返回全量规范化快照（父子关系、删除标记、属性、索引），
  测试用 `snapshot() == before` 断言"恢复后与删除前完全一致"。
- `check_invariants()` 自检内部不变量：父子结构、children 表、属性索引三者一致，
  活节点无删除痕迹，已删除节点祖先链完整。

## 确定性约定

- 重复删除 → `AlreadyDeletedError`；删除/恢复不存在节点 → `NotFoundError`
- 恢复未删除节点 → `NotDeletedError`；purge 未删除节点 → `NotInTrashError`
- 非级联删除有活子节点 → `HasLiveChildrenError`；清除后恢复 → `NotFoundError`

## 测试覆盖

- 边界：空树、单节点、深层嵌套（2000 层链，迭代实现无递归溢出）
- 三种父节点缺失策略（父节点被 purge / 父节点仅在回收站，均覆盖）
- 重复删除、删除-恢复循环、清除后恢复、级联删除中途失败回滚
- 规模：15101 节点批量级联删除并整体恢复（快照一致）、12000 节点批量清除
