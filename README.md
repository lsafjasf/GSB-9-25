# Versioned Snapshot Store

内存数据结构的版本化快照库（纯 Python 3 标准库，无第三方依赖）。

## 设计

- 每层嵌套 map 是一棵 **HAMT**（hash array mapped trie，32 路分叉）持久化数据结构。
- `snapshot()` 为 **O(1)**：只固定当前根节点引用。
- 写入沿路径写时复制（copy-on-write），代价为 `O(路径深度 × log32(每层map大小))`，
  **与变化量相关，与总数据量无关**；未变化的子树在所有版本间共享。
- 旧版本永不被原地修改 ⇒ 版本间强隔离。
- `delete_version()` 仅丢弃根引用，独占节点由引用计数自动回收
  （`_Counted.live_count` 可观测存活节点数）。

## API

```python
from versioned_store import VersionedStore

s = VersionedStore.from_dict({"users": {"alice": {"age": 30}}})
v0 = s.snapshot()
s.set(("users", "alice", "age"), 31)     # 修改
s.set(("users", "bob", "age"), 25)       # 新增
s.delete(("users", "alice", "age"))      # 删除
v1 = s.snapshot()
s.diff(v0, v1)        # {"added": [...], "removed": [...], "modified": [...]}
s.checkout(v0)        # 回退到任意版本
s.delete_version(v1)  # 删除中间版本并回收其独占数据
s.get(("users", "alice", "age"), v0)     # 读指定版本
s.to_dict(v0)                            # 物化为普通 dict
```

## 迭代新增：字段级差异、增量回退、保留策略

```python
# 1) 逐字段差异（带旧值/新值，可逐字段核对）
entries = s.diff_fields(v0, v1)
# [{"path": ("users", "alice", "age"), "op": "modified", "old": 30, "new": 31},
#  {"path": ("users", "bob", "age"),   "op": "removed",  "old": 25}, ...]

# 2) 增量回退：只重放差异条目，不重建整份数据；
#    默认逐字段校验结果与全量重建（to_dict(version)）一致
s.rollback_to(v0)     # {"replayed": n, "nodes_allocated": m, "verified": True}
s.apply_entries(entries)               # 也可手动把 diff 重放到工作区

# 3) 快照保留策略：按数量 + 时间自动清理，清理前给出影响范围
s.pin(v0)             # 标记仍被引用的版本（清理与 delete_version 都会跳过）
s.retention_plan(max_count=3, max_age=4 * 3600)   # 干跑：保留/删除清单及原因、
                                                  # 精确到节点的释放影响范围
s.apply_retention(max_count=3, max_age=4 * 3600)  # 执行清理；pinned 与最新版本
                                                  # 永远不会被删除
```

## 运行

```bash
python3 -m unittest test_versioned_store -v   # 隔离 / 回收 / diff / 增量回退 / 保留策略等 21 项测试
python3 benchmark.py                          # 全量复制 vs 增量快照耗时对比
python3 verify_iteration.py                   # 本迭代的端到端验证（差异核对 / 增量回退一致性 / 清理影响范围）
```

## 开销对比（本机实测，200 个快照 × 每轮 100 次修改）

| 数据量 | 全量深拷贝 | 增量快照 | 加速比 |
| ---: | ---: | ---: | ---: |
| 10,000 | 4.881s | 0.040s | 121x |
| 50,000 | 32.831s | 0.062s | 532x |
| 200,000 | 148.446s | 0.066s | 2264x |

数据量放大 20 倍时，增量快照耗时基本不变（变化量不变），全量复制线性增长。
200k 规模下 200 个版本共存活约 105 万个 HAMT 节点，而全量复制需持有约 4000 万行拷贝。
