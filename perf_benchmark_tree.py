"""多级分摊性能基准：集团 -> 部门 -> 员工，百万员工规模。

运行：python3 perf_benchmark_tree.py [员工数，默认 1000000] [部门数，默认 100]
"""

import random
import resource
import sys
import time

sys.path.insert(0, "src")
from allocation_tree import Node, allocate_tree, assert_tree_invariants, remainder_carriers


def main():
    n_staff = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    n_dept = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    rng = random.Random(20260929)
    total = 10**12  # 1 万亿元（分）

    t0 = time.perf_counter()
    depts = []
    for d in range(n_dept):
        k = n_staff // n_dept + (1 if d < n_staff % n_dept else 0)
        staff = tuple(
            Node(rng.randint(0, 10_000), (), f"员工{d}-{i}") for i in range(k)
        )
        depts.append(Node(rng.randint(1, 1000), staff, f"部门{d}"))
    root = Node(1, tuple(depts), "集团")
    build_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    result = allocate_tree(total, root)
    alloc_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    assert_tree_invariants(result, total)
    carriers = remainder_carriers(result)
    check_s = time.perf_counter() - t0

    leaf_sum = sum(c.amount for d in result.children for c in d.children)
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    print(f"员工数        = {n_staff:,}（{n_dept} 个部门，三层树）")
    print(f"总额 total    = {total:,} 分")
    print(f"建树耗时      = {build_s:.3f} s")
    print(f"分摊耗时      = {alloc_s:.3f} s")
    print(f"吞吐          = {n_staff / alloc_s:,.0f} 份/s")
    print(f"校验耗时      = {check_s:.3f} s（不变量 + 余数承担者汇总）")
    print(f"峰值内存      = {peak_kb / 1024:.1f} MiB")
    print(f"叶子层之和    = {leaf_sum:,}（严格等于总额: {leaf_sum == total}）")
    for depth in sorted(carriers):
        print(f"第{depth}级余数承担者 = {len(carriers[depth]):,} 份")
    print("不变量校验    = 通过（逐节点子级之和 == 上拨金额、零权重分得零、符号一致）")


if __name__ == "__main__":
    main()
