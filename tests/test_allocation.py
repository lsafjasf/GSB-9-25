"""修复版分摊函数的不变量断言测试与边界测试。运行：python3 -m unittest discover -s tests -v"""

import random
import sys
import unittest
from collections import Counter

sys.path.insert(0, "src")
from allocation import Share, allocate


def amounts(shares):
    return [s.amount for s in shares]


def multiset(shares):
    return Counter((s.weight, s.amount) for s in shares)


class Invariants(unittest.TestCase):
    """对随机与定向用例断言四条不变量。"""

    def check_invariants(self, total, weights):
        shares = allocate(total, weights)
        # 不变量 1：各份之和严格等于总额
        self.assertEqual(sum(amounts(shares)), total,
                         f"sum != total: total={total} weights={weights}")
        # 不变量 2：权重为零者分得零
        for s in shares:
            if s.weight == 0:
                self.assertEqual(s.amount, 0)
        # 不变量 4：正负号一致
        for s in shares:
            if total > 0:
                self.assertGreaterEqual(s.amount, 0)
            elif total < 0:
                self.assertLessEqual(s.amount, 0)
            else:
                self.assertEqual(s.amount, 0)
        # 不变量 3：结果与输入顺序无关（(权重,金额) 多重集合一致）
        rng = random.Random(12345)
        for _ in range(10):
            perm = list(range(len(weights)))
            rng.shuffle(perm)
            shuffled = [weights[i] for i in perm]
            self.assertEqual(multiset(allocate(total, shuffled)), multiset(shares),
                             f"顺序相关: total={total} weights={weights}")
        # 余数承担标记自洽：承担者少于份数、权重必为正、且每份只承担 1 个最小单位
        carriers = [s for s in shares if s.carried_remainder]
        self.assertLessEqual(len(carriers), len(weights))
        for s in carriers:
            self.assertGreater(s.weight, 0)
        if total != 0:
            self.assertTrue(all(s.amount != 0 for s in carriers))
        return shares

    def test_positive_amounts(self):
        for total, weights in [
            (100, [1, 1, 1]),
            (5, [1, 1, 1]),
            (101, [2, 2, 2, 2, 2]),
            (7, [1, 1, 1, 2]),
            (1, [1, 1]),          # 总额不够分，余数只落一份
            (10, [0, 0, 5]),      # 含零权重
            (1000000, [3, 1, 1]),
        ]:
            self.check_invariants(total, weights)

    def test_negative_amounts(self):
        for total, weights in [
            (-100, [1, 1, 1]),
            (-5, [1, 1, 1]),
            (-7, [1, 1, 1, 2]),
            (-1, [1, 1]),
            (-10, [0, 0, 5]),
        ]:
            self.check_invariants(total, weights)

    def test_tiny_weights(self):
        # 极小权重相对巨大权重和，仍满足全部不变量
        self.check_invariants(100, [1, 1, 10**12])
        self.check_invariants(3, [1] * 1000)
        self.check_invariants(-3, [1] * 1000)

    def test_random_fuzz(self):
        rng = random.Random(20260925)
        for _ in range(300):
            n = rng.randint(1, 30)
            weights = [rng.randint(0, 1000) for _ in range(n)]
            if all(w == 0 for w in weights):
                weights[rng.randrange(n)] = 1
            total = rng.randint(-10**6, 10**6)
            self.check_invariants(total, weights)


class EdgeCases(unittest.TestCase):
    def test_single_share(self):
        shares = allocate(12345, [7])
        self.assertEqual(amounts(shares), [12345])
        self.assertFalse(shares[0].carried_remainder)
        self.assertEqual(amounts(allocate(-999, [1])), [-999])

    def test_zero_total(self):
        shares = allocate(0, [3, 0, 5])
        self.assertEqual(amounts(shares), [0, 0, 0])
        self.assertTrue(all(not s.carried_remainder for s in shares))

    def test_zero_weights_with_zero_total(self):
        shares = allocate(0, [0, 0, 0])
        self.assertEqual(amounts(shares), [0, 0, 0])

    def test_all_zero_weights_nonzero_total_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            allocate(100, [0, 0, 0])
        self.assertIn("权重全为零", str(ctx.exception))

    def test_negative_weight_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            allocate(100, [3, -1, 2])
        msg = str(ctx.exception)
        self.assertIn("权重不允许为负", msg)
        self.assertIn("[1]", msg)  # 指出负权重下标

    def test_empty_weights(self):
        self.assertEqual(allocate(0, []), [])
        with self.assertRaises(ValueError):
            allocate(100, [])

    def test_audit_fields(self):
        shares = allocate(10, [1, 1, 1])
        self.assertTrue(all(isinstance(s, Share) for s in shares))
        self.assertEqual([s.index for s in shares], [0, 1, 2])
        self.assertEqual([s.weight for s in shares], [1, 1, 1])
        # 10 = 3+3+3 余 1，恰有一份承担余数且可定位
        carriers = [s.index for s in shares if s.carried_remainder]
        self.assertEqual(len(carriers), 1)
        self.assertEqual(shares[carriers[0]].amount, 4)

    def test_deterministic_rule_example(self):
        # 最大余数法可解释示例：total=10, weights=[1,2,7], W=10
        # 乘积 10/20/70 -> base 1/2/7，frac 全 0，无余数
        self.assertEqual(amounts(allocate(10, [1, 2, 7])), [1, 2, 7])
        # total=11, weights=[1,1,1]：frac 均为 1/3 并列，下标小者优先
        shares = allocate(11, [1, 1, 1])
        self.assertEqual(amounts(shares), [4, 4, 3])
        self.assertEqual([s.carried_remainder for s in shares], [True, True, False])
        # 负总额镜像：余数方向为 -1
        shares = allocate(-11, [1, 1, 1])
        self.assertEqual(amounts(shares), [-4, -4, -3])

    def test_tie_break_by_weight(self):
        # frac 并列时权重大者优先：total=3, weights=[1,3,2], W=6
        # 乘积 3/9/6 -> base 0/1/1, frac 3/3/0 -> 余数 1，
        # frac=3 在下标 0(w=1) 与下标 1(w=3) 并列，权重大者（下标 1）胜出
        shares = allocate(3, [1, 3, 2])
        self.assertEqual(amounts(shares), [0, 2, 1])
        self.assertEqual([s.carried_remainder for s in shares], [False, True, False])

    def test_tie_break_by_index(self):
        # frac 与权重均并列时下标小者优先：total=2, weights=[3,1,1], W=5
        # 乘积 6/2/2 -> base 1/0/0, frac 1/2/2 -> 余数 1，下标 1、2 并列，下标 1 胜出
        shares = allocate(2, [3, 1, 1])
        self.assertEqual(amounts(shares), [1, 1, 0])


if __name__ == "__main__":
    unittest.main()
