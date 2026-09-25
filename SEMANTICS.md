# 迭代可见性语义说明：快照式（Snapshot）

本实现明确选择 **快照式语义**：

> 调用 `items()` / `keys()` / `values()` / `__iter__()` 获取迭代器的瞬间，
> 对当时表中所有 **活跃记录** 拍一张不可变快照；该迭代器之后只遍历这张快照，
> 与实时表彻底解耦。

## 选择快照式而非实时式的理由

边遍历边修改时，实时式语义必须回答“刚删除的键若在探测链前方重新插入是否再访问”
“插入到尚未探测的槽位是否访问”等问题，极易出现漏访问与重复访问（即本次四类
现网缺陷的根源）。快照式给出一条简单且可严格验证的规则：**快照中的每条活跃
记录恰好访问一次（exactly-once），不多不少；快照之外的任何变化不可见。**

## 语义契约

| 迭代期间对实时表的操作 | 已打开的快照迭代器表现 |
| --- | --- |
| 删除当前记录 | 当前记录已在快照中，照常继续，下一条不丢 |
| 删除尚未访问的记录 | 快照里仍保留它，本次遍历照常访问 |
| 删除已访问记录后再插入同一键 | 本次遍历不会二次访问；下次新建迭代器可见 |
| 插入全新键 | 本次遍历不可见 |
| 更新某键的值 | 本次遍历看到快照时的旧值 |
| `compact()` / 扩容 rehash | 旧迭代器继续按快照正常遍历到底，不失效、不读已释放内存 |
| `clear()` | 旧迭代器仍能遍历完快照；实时表立即变空 |

快照持有的是私有的 `(keys_tuple, values_tuple)`，不引用实时表的探测数组，
因此实时表删除/搬迁后释放旧数组不会造成任何“读到已释放数据”。

注意：快照语义隔离的是 **键的存在性与值引用**；用户 value 对象自身若可变，
快照与实时表共享同一个对象（与 Python 容器遍历的常规行为一致），快照不做
深拷贝。

## 资源释放

- 快照迭代器在 **遍历正常结束**、**显式 `close()`**、**作为上下文管理器退出**
  或 **被垃圾回收** 时释放快照缓冲；
- 支持 `with index.items() as it: ...`，异常退出也会释放；
- `stats()["active_iterators"]` 实时返回尚未关闭的迭代器数量，便于观测；
- 长时间重复遍历不会积累游标/快照/临时缓冲，实测数据见下。

## 失效策略

按任务要求“旧迭代器在清理后要么继续按语义工作，要么给出明确失效错误”，本实现
选择前者：清理（`compact`/rehash/`clear`）后旧迭代器 **继续按快照语义工作**。
此外对已显式关闭的迭代器再调用 `next()`，会抛出明确的
`IteratorClosedError`（而不是读到脏数据或产生未定义行为）。

## 统计不变量

`stats()` 在任意时刻满足（`check_invariants()` 全表扫描断言）：

- `entries == 实际扫描到的活跃记录数 == len(index)`；
- `deleted == tombstones == 实际扫描到的墓碑数`；
- `entries + tombstones <= capacity`；
- `capacity` 为 2 的幂且不小于最小容量；
- 墓碑槽不保留任何 value 引用（删除即释放用户值）；
- 活跃键全局唯一（不存在重复键）；
- `compact()` 后 `deleted == 0`；`clear()` 后所有计数归零。

“已删除数”定义为 **当前表中尚未被整理的墓碑数**（是现存资源，不是累计历史
计数）；`compact()` 后归零。这样它在任何时刻都与实际一致。

## 内存实测数据

环境：Python 3.12.3，Linux，`tracemalloc`；表先 `compact()` 到稳定容量
（测量期间不发生 rehash）。运行 `python3 bench/memory_benchmark.py`：

```
rows per table             : 5000
measured traversals        : 3000
open iterators after run   : 0
steady-state baseline      : 160 B
memory after 3000 traversals : 192 B
  -> drift                 : 32 B (0.01 bytes/traversal)
one open snapshot view     : 78.4 KiB (single bounded copy)
drift from 500 nested      : 64 B
memory after snapshot close: 256 B (delta vs baseline 96 B)
```

- 3000 次完整长遍历后总漂移 32 B（均摊 0.01 B/次），无随遍历次数增长的资源；
- 单个打开的快照视图是一次性的有界拷贝（约 78.4 KiB / 5000 条，约 16 B/条，
  即键/值两个 tuple 的引用槽），与遍历次数无关；
- 快照关闭后内存回落到基线附近。

## 文件

- `src/memory_index.py`：修复后的索引（开放寻址 + 线性探测 + 墓碑 + 快照迭代器）。
- `src/index_buggy.py`：修复前的缺陷基线，仅用于复现，不要用于生产。
- `tests/conformance.py`：四个缺陷场景 + 统计一致性场景的共享驱动器（按快照
  语义判定）。
- `tests/test_repro_buggy.py`：对缺陷基线稳定复现四类问题的用例。
- `tests/test_memory_index.py`：修复版回归、快照语义、资源释放、内存不增长、
  不变量与多线程交错测试。
- `bench/memory_benchmark.py`：内存基准。
- `reproduce.py`：新旧实现逐条对比演示。
