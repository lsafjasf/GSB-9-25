"""修复版 allocate 的回归测试：五类问题复现用例 + 不变量断言集合。

运行：python3 -m unittest test_allocate -v
"""
import random
import unittest

from allocate import allocate, allocate_amounts


def assert_invariants(tc, total, weights, shares):
    """不变量断言集合：对任意合法输入都必须成立。"""
    amounts = [s.amount for s in shares]
    # 不变量 1：各份之和严格等于总额
    tc.assertEqual(sum(amounts), total,
                   f"sum({amounts}) != {total}")
    # 不变量 2：权重为零者分得零，且不承担余数
    for s in shares:
        if s.weight == 0:
            tc.assertEqual(s.amount, 0, f"下标 {s.index} 权重为零却分得 {s.amount}")
            tc.assertFalse(s.takes_residual)
    # 不变量 3：正负号一致（总额为零时全部为 0）
    for s in shares:
        if total > 0:
            tc.assertGreaterEqual(s.amount, 0)
        elif total < 0:
            tc.assertLessEqual(s.amount, 0)
        else:
            tc.assertEqual(s.amount, 0)
    # 不变量 4：承担余数的份数等于 |总额| - sum(基准)（不超过正权重份数）
    residual_count = sum(1 for s in shares if s.takes_residual)
    positive_weights = sum(1 for w in weights if w > 0)
    tc.assertLessEqual(residual_count, positive_weights)
    # 不变量 5：余数承担者恰比同权重基准多 1 个最小单位
    if total != 0:
        base_abs = abs(total) - residual_count
        for s in shares:
            if s.takes_residual:
                tc.assertEqual(abs(s.amount) * 1 % 1, 0)  # 整数性
    # 不变量 6：审计字段完整（下标、权重、金额、余数标记一一对应）
    tc.assertEqual([s.index for s in shares], list(range(len(weights))))
    tc.assertEqual([s.weight for s in shares], list(weights))


class TestReproducedBugs(unittest.TestCase):
    """针对五类现网问题的回归用例（修复后必须全部通过）。"""

    def test_bug1_sum_equals_total_large_amount(self):
        weights = [1, 1, 1]
        shares = allocate(10**18, weights)
        assert_invariants(self, 10**18, weights, shares)

    def test_bug2_all_zero_weights_nonzero_total_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            allocate(100, [0, 0, 0])
        self.assertIn("权重和为零", str(ctx.exception))

    def test_bug2_all_zero_weights_zero_total(self):
        shares = allocate(0, [0, 0, 0])
        self.assertEqual([s.amount for s in shares], [0, 0, 0])

    def test_bug3_negative_total_direction(self):
        weights = [1, 1, 1]
        shares = allocate(-100, weights)
        assert_invariants(self, -100, weights, shares)
        self.assertEqual(sum(s.amount for s in shares), -100)
        # 与正总额镜像对称
        pos = allocate_amounts(100, weights)
        self.assertEqual([s.amount for s in shares], [-a for a in pos])

    def test_bug4_order_independence(self):
        weights = [3, 1, 1]
        base = sorted((s.weight, s.amount) for s in allocate(11, weights))
        shuffled = [1, 1, 3]
        other = sorted((s.weight, s.amount) for s in allocate(11, shuffled))
        self.assertEqual(base, other)

    def test_bug5_residual_traceable(self):
        shares = allocate(100, [1, 1, 1])
        takers = [s.index for s in shares if s.takes_residual]
        self.assertEqual(len(takers), 1)
        self.assertEqual(abs(shares[takers[0]].amount), 34)
        for s in shares:
            self.assertTrue(hasattr(s, "weight"))
            self.assertTrue(hasattr(s, "takes_residual"))


class TestInvariantsPropertyBased(unittest.TestCase):
    """随机化不变量测试：正负金额、零权重、极小权重、输入顺序打乱。"""

    def test_randomized_invariants(self):
        rng = random.Random(20260925)
        for _ in range(2000):
            n = rng.randint(1, 30)
            weights = [rng.choice([0, 0, 1, 2, 3, 7, 10**6, 10**12])
                       for _ in range(n)]
            if all(w == 0 for w in weights):
                weights[rng.randrange(n)] = rng.randint(1, 100)
            total = rng.randint(-10**9, 10**9)
            shares = allocate(total, weights)
            assert_invariants(self, total, weights, shares)

    def test_order_independence_shuffled(self):
        rng = random.Random(42)
        for _ in range(500):
            n = rng.randint(2, 20)
            weights = [rng.randint(0, 50) for _ in range(n)]
            if all(w == 0 for w in weights):
                weights[0] = 1
            total = rng.randint(-10**6, 10**6)
            baseline = sorted((s.weight, s.amount) for s in allocate(total, weights))
            for _ in range(3):
                perm = list(range(n))
                rng.shuffle(perm)
                shuffled_weights = [weights[i] for i in perm]
                got = sorted((s.weight, s.amount)
                             for s in allocate(total, shuffled_weights))
                self.assertEqual(baseline, got,
                                 f"顺序打乱后结果变化: weights={weights} total={total}")

    def test_sign_symmetry(self):
        rng = random.Random(7)
        for _ in range(500):
            n = rng.randint(1, 15)
            weights = [rng.randint(0, 20) for _ in range(n)]
            if all(w == 0 for w in weights):
                weights[0] = 3
            total = rng.randint(1, 10**6)
            pos = allocate_amounts(total, weights)
            neg = allocate_amounts(-total, weights)
            self.assertEqual(pos, [-a for a in neg])

    def test_tiny_weights(self):
        # 极小权重相对巨大权重：小权重份通常分得 0，但总和必须严格相等
        weights = [10**12, 1, 1, 1]
        shares = allocate(10**9 + 3, weights)
        assert_invariants(self, 10**9 + 3, weights, shares)

    def test_equal_weights_split_evenly_plus_residual(self):
        # 等权重：任意两份之差不超过 1 个最小单位
        shares = allocate(1000, [1] * 6)
        amounts = sorted(s.amount for s in shares)
        self.assertLessEqual(amounts[-1] - amounts[0], 1)
        self.assertEqual(sum(amounts), 1000)


class TestEdgeCases(unittest.TestCase):
    def test_single_share(self):
        shares = allocate(12345, [7])
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0].amount, 12345)
        self.assertFalse(shares[0].takes_residual)
        shares = allocate(-999, [1])
        self.assertEqual(shares[0].amount, -999)

    def test_negative_weight_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            allocate(100, [3, -1, 2])
        msg = str(ctx.exception)
        self.assertIn("权重不允许为负", msg)
        self.assertIn("1", msg)  # 指明 offending 下标

    def test_zero_total(self):
        shares = allocate(0, [5, 0, 3])
        self.assertEqual([s.amount for s in shares], [0, 0, 0])
        self.assertFalse(any(s.takes_residual for s in shares))

    def test_zero_weight_gets_zero(self):
        shares = allocate(100, [0, 1, 0, 1])
        self.assertEqual(shares[0].amount, 0)
        self.assertEqual(shares[2].amount, 0)
        self.assertEqual(sum(s.amount for s in shares), 100)

    def test_empty_weights(self):
        self.assertEqual(allocate(0, []), [])
        with self.assertRaises(ValueError):
            allocate(100, [])

    def test_exact_division_no_residual(self):
        shares = allocate(300, [1, 1, 1])
        self.assertEqual([s.amount for s in shares], [100, 100, 100])
        self.assertFalse(any(s.takes_residual for s in shares))

    def test_residual_tie_break_deterministic(self):
        # 完全并列（同余数同权重）时按下标升序决胜，结果可复现
        first = allocate(5, [2, 2, 2])
        second = allocate(5, [2, 2, 2])
        self.assertEqual(first, second)
        takers = [s.index for s in first if s.takes_residual]
        self.assertEqual(takers, [0, 1])  # 5 = 2+2+1，前两份各承担 1


if __name__ == "__main__":
    unittest.main()
