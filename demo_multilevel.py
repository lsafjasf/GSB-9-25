"""多级分摊业务示例：总额 -> 部门 -> 员工，输出每级余数承担者并验证不变量。

运行：python3 demo_multilevel.py
"""

import random
import sys
from collections import Counter

sys.path.insert(0, "src")
from allocation_tree import (
    Node,
    allocate_tree,
    assert_tree_invariants,
    fingerprint,
    levels,
    remainder_carriers,
)


def leaf(weight, label):
    return Node(weight, (), label)


def build_tree():
    return Node(1, (
        Node(3, (leaf(1, "甲"), leaf(1, "乙"), leaf(2, "丙")), "华东部"),
        Node(2, (leaf(1, "丁"), leaf(0, "戊")), "华西部"),  # 戊：零权重（如离职冻结）
        Node(5, (leaf(2, "己"), leaf(3, "庚"), leaf(3, "辛")), "华南区"),
    ), "集团")


def show(result, total):
    print(f"总额 {total:,} 分，逐级分摊结果：")
    for depth, lvl in enumerate(levels(result)):
        level_sum = sum(n.amount for n in lvl)
        names = "、".join(f"{n.label}={n.amount:,}" for n in lvl)
        print(f"  第{depth}级（和={level_sum:,}）: {names}")
    print("  余数承担者（可追溯）：")
    carriers = remainder_carriers(result)
    if not carriers:
        print("    （无余数）")
    for depth in sorted(carriers):
        for path, unit in carriers[depth]:
            print(f"    第{depth}级 {path} 承担余数 {unit:+d} 分")


def fingerprinted_multiset(result, root):
    out = Counter()

    def walk(r, n):
        out[(fingerprint(n), r.amount, r.carried_remainder)] += 1
        for rc, nc in zip(r.children, n.children):
            walk(rc, nc)

    walk(result, root)
    return out


def shuffle_tree(node, rng):
    children = list(node.children)
    rng.shuffle(children)
    return Node(node.weight, tuple(shuffle_tree(c, rng) for c in children), node.label)


def main():
    total = 1_000_003  # 10,000.03 元
    root = build_tree()

    result = allocate_tree(total, root)
    show(result, total)
    assert_tree_invariants(result, total)
    print("  不变量校验：通过（每级之和严格等于上拨金额）")

    print()
    neg = allocate_tree(-total, root)
    print(f"负总额镜像校验：sum = {sum(n.amount for n in levels(neg)[-1]):,}（应 {-total:,}）")
    assert_tree_invariants(neg, -total)
    print("  通过")

    print()
    rng = random.Random(20260929)
    baseline = fingerprinted_multiset(result, root)
    for trial in range(100):
        shuffled_root = shuffle_tree(root, rng)
        shuffled = allocate_tree(total, shuffled_root)
        assert fingerprinted_multiset(shuffled, shuffled_root) == baseline
    print("顺序无关校验：打乱输入顺序 100 次，每个节点金额完全一致，通过")


if __name__ == "__main__":
    main()
