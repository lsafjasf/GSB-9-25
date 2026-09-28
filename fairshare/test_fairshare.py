"""不变量断言测试 + 边界情形 + 不可行用例 + 顺序无关性。运行: python3 -m unittest -v"""

import itertools
import random
import unittest

from fairshare import InfeasibleError, allocate


def assert_invariants(tc, capacity, weights, demands, minimums, alloc):
    """核心不变量：守恒、上限、下限、零权重、确定性。"""
    n = len(weights)
    tc.assertEqual(len(alloc), n)
    # 守恒：分配之和等于可分配总量（零权重租户申请不可达，不计入）
    reachable = sum(d if w > 0 else m for w, d, m in zip(weights, demands, minimums))
    tc.assertEqual(sum(alloc), min(capacity, reachable))
    for i in range(n):
        tc.assertLessEqual(alloc[i], demands[i], f"#{i} exceeds demand")
        tc.assertGreaterEqual(alloc[i], minimums[i], f"#{i} below minimum")
        if weights[i] == 0:
            tc.assertEqual(alloc[i], 0, f"#{i} zero-weight nonzero alloc")
    # 确定性：同输入再跑一次必须一致
    again = allocate(capacity, weights, demands, minimums)
    tc.assertEqual(alloc, again)


class TestEdgeCases(unittest.TestCase):
    def test_single_tenant(self):
        alloc = allocate(100, [5], [40])
        self.assertEqual(alloc, [40])  # 不超过申请量
        alloc = allocate(100, [5], [200])
        self.assertEqual(alloc, [100])  # 独占全部容量

    def test_total_demand_below_capacity(self):
        w = [1, 2, 3]
        d = [10, 20, 30]
        alloc = allocate(1000, w, d)
        self.assertEqual(alloc, d)  # 人人满足，守恒于总申请
        assert_invariants(self, 1000, w, d, [0] * 3, alloc)

    def test_all_zero_demands(self):
        alloc = allocate(100, [1, 2, 3], [0, 0, 0])
        self.assertEqual(alloc, [0, 0, 0])
        self.assertEqual(sum(alloc), 0)

    def test_equal_weights_split_evenly(self):
        alloc = allocate(100, [1, 1, 1], [200, 200, 200])
        self.assertEqual(alloc, [34, 33, 33])  # 尽量均分，余数确定地给索引小者
        assert_invariants(self, 100, [1, 1, 1], [200, 200, 200], [0] * 3, alloc)

    def test_extreme_weight_ratio(self):
        w = [1_000_000, 1]
        d = [10**9, 10**9]
        alloc = allocate(10**6 + 1, w, d)
        self.assertGreater(alloc[0], alloc[1])
        self.assertLess(alloc[1], 10)  # 极小权重者份额极小但确定
        assert_invariants(self, 10**6 + 1, w, d, [0] * 2, alloc)

    def test_zero_weight_gets_zero(self):
        alloc = allocate(100, [0, 1], [50, 200])
        self.assertEqual(alloc, [0, 100])

    def test_minimum_guarantee_respected(self):
        w = [10, 1, 1]
        d = [100, 100, 100]
        m = [0, 30, 30]
        alloc = allocate(90, w, d, m)
        self.assertGreaterEqual(alloc[1], 30)
        self.assertGreaterEqual(alloc[2], 30)
        assert_invariants(self, 90, w, d, m, alloc)

    def test_guarantee_consumes_most_capacity(self):
        w = [1, 1]
        d = [80, 80]
        m = [45, 45]
        alloc = allocate(95, w, d, m)
        self.assertEqual(alloc, [48, 47])  # 保障 90 + 剩余 5 均分，余数按确定规则给索引小者
        assert_invariants(self, 95, w, d, m, alloc)

    def test_zero_capacity(self):
        alloc = allocate(0, [1, 2], [10, 10])
        self.assertEqual(alloc, [0, 0])


class TestInfeasible(unittest.TestCase):
    def test_minimums_exceed_capacity(self):
        w = [1, 1, 1]
        d = [100, 100, 100]
        m = [40, 40, 30]  # 总和 110 > 容量 100
        with self.assertRaises(InfeasibleError) as ctx:
            allocate(100, w, d, m)
        err = ctx.exception
        self.assertEqual(err.deficit, 10)  # 明确报出缺口
        self.assertEqual(err.conflicts, [(0, 40), (1, 40), (2, 30)])  # 冲突方
        self.assertIn("deficit=10", str(err))

    def test_minimum_exceeds_own_demand(self):
        # 保障大于自身申请量属于非法输入（ValueError），而非容量不可行
        with self.assertRaises(ValueError):
            allocate(100, [1], [10], [20])
        with self.assertRaises(ValueError):
            allocate(100, [1, 1], [10, 50], [0, 60])

    def test_zero_weight_with_positive_minimum(self):
        with self.assertRaises(InfeasibleError):
            allocate(100, [0, 1], [50, 50], [10, 0])


class TestRandomizedInvariants(unittest.TestCase):
    def test_random_instances_hold_invariants(self):
        rng = random.Random(20260925)
        for trial in range(300):
            n = rng.randint(1, 50)
            weights = [rng.choice([0, 1, 2, 5, 100, 10**6]) for _ in range(n)]
            demands = [rng.randint(0, 500) for _ in range(n)]
            minimums = []
            for i in range(n):
                if weights[i] == 0:
                    minimums.append(0)
                else:
                    minimums.append(rng.randint(0, demands[i]))
            capacity = rng.randint(sum(minimums), sum(minimums) + 2000)
            alloc = allocate(capacity, weights, demands, minimums)
            assert_invariants(self, capacity, weights, demands, minimums, alloc)


class TestOrderIndependence(unittest.TestCase):
    """余数归属只取决于租户属性（小数部分、权重），与输入顺序无关。"""

    def test_fraction_tie_goes_to_larger_weight(self):
        # 小数部分并列（均为 0.5）时，余数归权重较大者，而非索引较小者
        self.assertEqual(allocate(6, [1, 3], [10, 10]), [1, 5])
        self.assertEqual(allocate(6, [3, 1], [10, 10]), [5, 1])

    def test_equal_weights_permutation_invariant(self):
        # 同权重同输入：任意打乱租户顺序，分配结果（多重集）不变
        weights = [1, 1, 1, 1]
        demands = [200, 150, 100, 50]
        minimums = [10, 0, 20, 0]
        capacity = 123
        alloc = allocate(capacity, weights, demands, minimums)
        for perm in itertools.permutations(range(4)):
            w = [weights[i] for i in perm]
            d = [demands[i] for i in perm]
            m = [minimums[i] for i in perm]
            shuffled = allocate(capacity, w, d, m)
            self.assertEqual(sorted(shuffled), sorted(alloc),
                             f"permutation {perm} changed the outcome")

    def test_random_permutations_preserve_allocations(self):
        # 随机用例（含同权重租户）：打乱顺序后
        # 1) 分配多重集不变；2) 权重唯一的租户份额跟随租户而非位置
        rng = random.Random(20260929)
        for _ in range(200):
            n = rng.randint(2, 8)
            weights = [rng.choice([0, 1, 1, 2, 3, 5]) for _ in range(n)]
            demands = [rng.randint(0, 30) for _ in range(n)]
            minimums = [
                0 if w == 0 else rng.randint(0, d)
                for w, d in zip(weights, demands)
            ]
            capacity = rng.randint(sum(minimums), sum(minimums) + 100)
            alloc = allocate(capacity, weights, demands, minimums)

            perm = list(range(n))
            rng.shuffle(perm)
            w = [weights[i] for i in perm]
            d = [demands[i] for i in perm]
            m = [minimums[i] for i in perm]
            shuffled = allocate(capacity, w, d, m)

            self.assertEqual(sorted(shuffled), sorted(alloc))
            for pos, i in enumerate(perm):
                if weights.count(weights[i]) == 1:
                    self.assertEqual(shuffled[pos], alloc[i],
                                     f"unique-weight tenant #{i} got "
                                     f"{shuffled[pos]} after shuffle, "
                                     f"was {alloc[i]}")


if __name__ == "__main__":
    unittest.main()
