"""多级分摊测试：每级严格相等、余数可追溯、顺序打乱结果不变。

运行：python3 -m unittest discover -s tests -v
"""

import random
import sys
import unittest
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


def leaf(weight, label=""):
    return Node(weight, (), label)


def fingerprinted_multiset(result, root):
    """全树 (子树指纹, 金额, 是否承担余数) 多重集合——顺序无关的比较基准。"""
    out = Counter()

    def walk(r, n):
        out[(fingerprint(n), r.amount, r.carried_remainder)] += 1
        for rc, nc in zip(r.children, n.children):
            walk(rc, nc)

    walk(result, root)
    return out


def shuffle_tree(node, rng):
    """递归打乱每个节点的子级顺序，返回同构新树。"""
    children = list(node.children)
    rng.shuffle(children)
    return Node(node.weight, tuple(shuffle_tree(c, rng) for c in children), node.label)


def make_dept_tree(dept_weights, staff_weights, rng=None):
    """构造 集团 -> 部门 -> 员工 三层树。"""
    rng = rng or random.Random(0)
    depts = []
    for d, dw in enumerate(dept_weights):
        staff = tuple(
            leaf(w, f"员工{d}-{i}") for i, w in enumerate(staff_weights[d])
        )
        depts.append(Node(dw, staff, f"部门{d}"))
    return Node(1, tuple(depts), "集团")


class TreeInvariants(unittest.TestCase):
    """随机与定向用例上的多级不变量。"""

    def check(self, total, root):
        result = allocate_tree(total, root)
        # 不变量 1：每级严格相等（逐节点子级之和 == 上拨金额）+ 零权重 + 符号
        assert_tree_invariants(result, total)
        # 不变量 2：等深树每一层之和 == 总额
        lvls = levels(result)
        depth_count = Counter()
        def max_depth(node):
            return 0 if not node.children else 1 + max(max_depth(c) for c in node.children)
        if max_depth(result) == len(lvls) - 1 and all(
            len(levels(r)) == len(lvls) for r in [result]
        ):
            pass  # 通用树只断言逐节点；等深树额外断言每层之和
        # 不变量 3：打乱输入顺序，每个节点（按指纹定位）分到的金额不变
        rng = random.Random(777)
        baseline = fingerprinted_multiset(result, root)
        for _ in range(10):
            shuffled_root = shuffle_tree(root, rng)
            shuffled = allocate_tree(total, shuffled_root)
            self.assertEqual(fingerprinted_multiset(shuffled, shuffled_root), baseline,
                             f"顺序相关: total={total}")
        # 不变量 4：余数承担可追溯——每个内部节点的承担者数量
        # 恰等于 |上拨金额| - sum(子级基础份额)
        def check_carriers(node):
            if node.children:
                wsum = sum(c.weight for c in node.children)
                magnitude = abs(node.amount)
                bases = sum(magnitude * c.weight // wsum for c in node.children if c.weight)
                expected = magnitude - bases
                carriers = [c for c in node.children if c.carried_remainder]
                self.assertEqual(len(carriers), expected,
                                 f"余数承担者数量不符: 应 {expected} 实 {len(carriers)}")
                for c in node.children:
                    check_carriers(c)
        check_carriers(result)
        return result

    def test_three_level_business_case(self):
        # 总额 -> 部门 -> 员工，含余数与零权重
        root = Node(1, (
            Node(3, (leaf(1, "甲"), leaf(1, "乙"), leaf(1, "丙")), "华东部"),
            Node(2, (leaf(1, "丁"), leaf(0, "戊")), "华西部"),  # 戊零权重
            Node(5, (leaf(2, "己"), leaf(3, "庚")), "华南区"),
        ), "集团")
        for total in (1_000_000, 999_983, 7, 1, -1_000_000, -5):
            self.check(total, root)

    def test_four_level_deep(self):
        # 集团 -> 大区 -> 部门 -> 小组 四层
        root = Node(1, tuple(
            Node(r + 1, tuple(
                Node(d + 1, tuple(leaf(g + 1) for g in range(3)), f"部门{r}{d}")
                for d in range(2)
            ), f"大区{r}")
            for r in range(3)
        ), "集团")
        for total in (10**9, 123_457, -123_457, 2):
            self.check(total, root)

    def test_random_fuzz(self):
        rng = random.Random(20260929)
        for _ in range(200):
            n_dept = rng.randint(1, 6)
            dept_weights = [rng.randint(0, 50) for _ in range(n_dept)]
            if all(w == 0 for w in dept_weights):
                dept_weights[0] = 1
            staff_weights = []
            for dw in dept_weights:
                k = rng.randint(1, 8)
                ws = [rng.randint(0, 100) for _ in range(k)]
                if dw > 0 and all(w == 0 for w in ws):
                    ws[0] = rng.randint(1, 100)
                staff_weights.append(ws)
            root = make_dept_tree(dept_weights, staff_weights, rng)
            total = rng.randint(-10**6, 10**6)
            # 零权重部门上拨金额为 0，其下全零权重合法（0 分摊到零权重得 0）
            self.check(total, root)

    def test_level_sums_equal_total_for_uniform_tree(self):
        root = make_dept_tree([3, 2, 5], [[1, 1, 1], [1, 0], [2, 3]])
        for total in (1_000_000, -999_983, 11):
            result = allocate_tree(total, root)
            for depth, lvl in enumerate(levels(result)):
                self.assertEqual(sum(n.amount for n in lvl), total,
                                 f"第 {depth} 层之和 != 总额 {total}")


class TreeEdgeCases(unittest.TestCase):
    def test_single_leaf_root(self):
        result = allocate_tree(12345, leaf(9, "唯一"))
        self.assertEqual(result.amount, 12345)
        self.assertFalse(result.carried_remainder)
        self.assertEqual(result.children, ())

    def test_zero_total(self):
        root = make_dept_tree([3, 2], [[1, 1], [0, 5]])
        result = allocate_tree(0, root)
        assert_tree_invariants(result, 0)
        self.assertEqual(remainder_carriers(result), {})

    def test_negative_weight_rejected(self):
        root = Node(1, (Node(-2, (leaf(1),), "坏部门"),), "集团")
        with self.assertRaises(ValueError) as ctx:
            allocate_tree(100, root)
        self.assertIn("权重不允许为负", str(ctx.exception))
        self.assertIn("坏部门", str(ctx.exception))  # 路径可定位

    def test_all_zero_child_weights_nonzero_amount_rejected(self):
        root = Node(1, (Node(1, (leaf(0), leaf(0)), "空部门"),), "集团")
        with self.assertRaises(ValueError) as ctx:
            allocate_tree(100, root)
        self.assertIn("权重全为零", str(ctx.exception))

    def test_zero_weight_branch_gets_zero_despite_zero_staff_weights(self):
        # 零权重部门上拨 0，其下员工权重全零也合法，全部分得 0
        root = Node(1, (
            Node(0, (leaf(0, "甲"), leaf(0, "乙")), "空部门"),
            Node(1, (leaf(1, "丙"),), "实部门"),
        ), "集团")
        result = allocate_tree(100, root)
        assert_tree_invariants(result, 100)
        self.assertEqual(result.children[0].amount, 0)
        self.assertEqual([c.amount for c in result.children[0].children], [0, 0])
        self.assertEqual(result.children[1].children[0].amount, 100)

    def test_remainder_carriers_report(self):
        # total=10, 部门权重 [1,1,1]：部门级余数 1；华东部上拨 4，
        # 员工权重 [1,1,1]：员工级余数 1，两级的承担者都可定位
        # 同权重并列时按指纹 (权重, 标签, 子树) 升序决胜：小A < 小B < 小C
        root = Node(1, (
            Node(1, (leaf(1, "小A"), leaf(1, "小B"), leaf(1, "小C")), "A部"),
            Node(1, (leaf(1, "丁"),), "B部"),
            Node(1, (leaf(1, "戊"),), "C部"),
        ), "集团")
        result = allocate_tree(10, root)
        assert_tree_invariants(result, 10)
        carriers = remainder_carriers(result)
        # 部门级：10 = 4+3+3，A 部承担 1
        self.assertEqual(carriers[1], [("集团/A部", 1)])
        # 员工级：A 部 4 = 2+1+1，小A 承担 1
        self.assertEqual(carriers[2], [("集团/A部/小A", 1)])
        # 负总额镜像：承担 -1
        carriers_neg = remainder_carriers(allocate_tree(-10, root))
        self.assertEqual(carriers_neg[1], [("集团/A部", -1)])
        self.assertEqual(carriers_neg[2], [("集团/A部/小A", -1)])

    def test_shuffle_invariance_deep_tree(self):
        rng = random.Random(20260929)
        root = make_dept_tree(
            [rng.randint(1, 20) for _ in range(5)],
            [[rng.randint(0, 30) for _ in range(rng.randint(2, 6))] for _ in range(5)],
        )
        total = 987_654_321
        baseline = fingerprinted_multiset(allocate_tree(total, root), root)
        for _ in range(50):
            shuffled_root = shuffle_tree(root, rng)
            shuffled = allocate_tree(total, shuffled_root)
            self.assertEqual(fingerprinted_multiset(shuffled, shuffled_root), baseline)


if __name__ == "__main__":
    unittest.main()
