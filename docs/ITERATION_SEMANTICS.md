# 迭代语义说明

## 选定的语义：快照式（Snapshot）

`MemoryIndex` 的迭代器在**创建时刻 T** 冻结可见视图：

- 迭代器恰好产出 T 时刻处于活跃状态的记录，**每条恰好一次**，不遗漏、不重复。
- T 之后的任何修改（`put` 新键、`delete`、`cleanup`）对该迭代器**不可见**：
  - 遍历中删除当前记录 → 后续记录照常访问，不会跳过；
  - 遍历中删除未访问记录 → 该记录仍会被访问（它在 T 时刻是活跃的）；
  - 遍历中删除后再插入同一键 → 仍只访问一次（快照内容不变）；
  - 遍历中插入的新键 → 不会被该迭代器访问，新迭代器才能看到。
- 快照冻结的是「键集合 + 值引用」。值对象本身若被外部修改，不属于索引的语义范围。

## 清理（cleanup）与旧迭代器

`cleanup()` 只替换索引自身的存储，不影响任何已存在的迭代器。
旧迭代器在 `cleanup()` 之后**继续按快照语义正常工作**，不会读到已释放的
数据，也不会抛出失效错误。（选择「继续可用」而非「失效报错」，因为快照
缓冲与索引存储完全解耦，没有失效的必要。）

## 资源释放

- 迭代器耗尽（`StopIteration`）时自动释放快照缓冲；
- 也可显式 `close()` 或使用上下文管理器（`with iter(idx) as it:`）提前释放；
- `idx.active_iterators` 可随时查询仍持有快照缓冲的迭代器数量，遍历结束后恒为 0；
- 内存数据：30 轮 × 50,000 条记录的全量遍历（遍历中夹杂删除/再插入，
  每 5 轮一次 `cleanup()`），tracemalloc 峰值约 **13.9 MiB**（单份快照的
  瞬时开销），遍历结束后容量无增长、`active_iterators == 0`。数据由
  `tests/test_memory_index.py::ResourceTests::test_long_traversal_memory_stable`
  实测输出。

## 统计接口与不变量

`stats()` 返回 `{"size", "deleted", "capacity"}`，任何时刻满足：

- `size` == 活跃记录数 == `len(idx)`；
- `deleted` == 尚未压缩的墓碑槽数（`cleanup()` 后归零）；
- `capacity` == `size` + `deleted`（槽位总数，删除的槽位会被后续 `put` 复用）。

`check_invariants()` 对上述不变量及「索引表 ↔ 槽位」一致性做断言，
测试在每次操作后调用，保证统计在任何时刻与实际一致。

## 线程安全

实现为单线程语义（「并发插入删除」指遍历期间交错修改）。多线程共享
同一索引时需要外部加锁。

## 运行命令

```bash
cd /home/administrator/gsb/uid13/B
python3 -m unittest discover -s tests -v        # 全部测试（复现 + 回归 + 内存）
python3 -m unittest tests.test_buggy_repro -v   # 仅四类缺陷的复现用例
python3 -m unittest tests.test_memory_index -v  # 仅修复后的回归/不变量/内存测试
```
