"""rectpack 约束断言测试。运行：python3 -m unittest test_rectpack -v"""

import importlib
import random
import unittest

from rectpack import Rect, pack, pack_naive


def assert_valid_packing(test_case, result, expected_count):
    """核心可断言约束：不重叠、在容器内、未放置被明确列出、账目平衡。"""
    placed = result.placed
    # 1) 均在容器内（含边界）
    for p in placed:
        test_case.assertGreaterEqual(p.x, 0)
        test_case.assertGreaterEqual(p.y, 0)
        test_case.assertLessEqual(p.x + p.w, result.width)
        test_case.assertLessEqual(p.y + p.h, result.height)
    # 2) 两两不重叠（扫描线：按 x 排序后只需检查活跃区间）
    events = sorted(placed, key=lambda p: p.x)
    active = []
    for p in events:
        active = [q for q in active if q.x + q.w > p.x]
        for q in active:
            overlap_y = p.y < q.y + q.h and q.y < p.y + p.h
            test_case.assertFalse(overlap_y,
                                  f"{p.rid} 与 {q.rid} 重叠")
        active.append(p)
    # 3) 未放置矩形被明确列出，且每个都有原因
    for u in result.unplaced:
        test_case.assertTrue(u.reason)
    # 4) 账目平衡：placed + unplaced == 输入总数，rid 不重复不丢失
    test_case.assertEqual(len(placed) + len(result.unplaced), expected_count)
    rids = [p.rid for p in placed] + [u.rid for u in result.unplaced]
    test_case.assertEqual(len(rids), len(set(rids)))
    # 5) 利用率在 (0, 1] 之间
    if placed:
        test_case.assertGreater(result.utilization, 0)
        test_case.assertLessEqual(result.utilization, 1.0 + 1e-9)


def random_rects(n, seed, lo=2, hi=40):
    rng = random.Random(seed)
    return [(rng.randint(lo, hi), rng.randint(lo, hi)) for _ in range(n)]


class TestConstraints(unittest.TestCase):
    def test_random_instances_satisfy_constraints(self):
        for seed in range(10):
            rects = random_rects(120, seed)
            for rot in (False, True):
                for fixed in (False, True):
                    res = pack(100, rects, allow_rotation=rot,
                               fixed_order=fixed)
                    assert_valid_packing(self, res, len(rects))

    def test_naive_baseline_also_valid(self):
        rects = random_rects(200, seed=99)
        res = pack_naive(100, rects, allow_rotation=True)
        assert_valid_packing(self, res, len(rects))

    def test_determinism(self):
        rects = random_rects(300, seed=7)
        for rot in (False, True):
            a = pack(80, rects, allow_rotation=rot)
            b = pack(80, rects, allow_rotation=rot)
            self.assertEqual(a.placed, b.placed)
            self.assertEqual(a.unplaced, b.unplaced)
            self.assertEqual(a.height, b.height)

    def test_rotation_marks_rotated(self):
        # 60x10 无法以原朝向放入宽 50 的容器，旋转后可放入
        res = pack(50, [Rect(60, 10, "wide")], allow_rotation=True)
        self.assertEqual(len(res.placed), 1)
        self.assertTrue(res.placed[0].rotated)
        self.assertEqual((res.placed[0].w, res.placed[0].h), (10, 60))

    def test_no_rotation_mode_does_not_rotate(self):
        res = pack(50, [Rect(60, 10, "wide")], allow_rotation=False)
        self.assertEqual(len(res.placed), 0)
        self.assertEqual(res.unplaced[0].reason, "exceeds container width")

    def test_fixed_order_respects_input_sequence(self):
        # 固定顺序：放置顺序严格等于输入顺序
        rects = [Rect(10, 10, "small_first"), Rect(50, 50, "big")]
        res = pack(100, rects, fixed_order=True)
        self.assertEqual([p.rid for p in res.placed],
                         ["small_first", "big"])
        # 非固定顺序：大矩形优先放置
        res2 = pack(100, rects, fixed_order=False)
        self.assertEqual(res2.placed[0].rid, "big")

    def test_oversized_rect_rejected_with_reason(self):
        # 200x150 旋转后仍为 150x200，两个方向都宽于容器
        rects = [Rect(200, 150, "too_wide"), Rect(10, 10, "ok")]
        res = pack(100, rects, allow_rotation=True)
        self.assertEqual([u.rid for u in res.unplaced], ["too_wide"])
        self.assertEqual(res.unplaced[0].reason, "exceeds container width")
        self.assertEqual([p.rid for p in res.placed], ["ok"])

    def test_zero_size_rect_rejected(self):
        with self.assertRaises(ValueError):
            pack(100, [(0, 10)])
        with self.assertRaises(ValueError):
            pack(100, [(10, 0)])
        with self.assertRaises(ValueError):
            pack(100, [(-5, 10)])
        with self.assertRaises(ValueError):
            pack_naive(100, [(0, 0)])

    def test_invalid_container_width_rejected(self):
        with self.assertRaises(ValueError):
            pack(0, [(10, 10)])
        with self.assertRaises(ValueError):
            pack(-3, [(10, 10)])

    def test_empty_input(self):
        res = pack(100, [])
        self.assertEqual(res.height, 0)
        self.assertEqual(res.placed, [])
        self.assertEqual(res.unplaced, [])
        self.assertEqual(res.utilization, 0.0)

    def test_exact_fit(self):
        res = pack(100, [(100, 50), (100, 50)])
        self.assertEqual(res.height, 100)
        self.assertAlmostEqual(res.utilization, 1.0)

    def test_rect_objects_and_tuples_mixed(self):
        res = pack(50, [Rect(20, 20, "a"), (20, 20)])
        self.assertEqual(len(res.placed), 2)
        self.assertEqual(res.placed[1].rid, "r1")  # 元组自动编号

    def test_scale_1000_rects_constraints(self):
        rects = random_rects(1000, seed=42, lo=1, hi=25)
        res = pack(200, rects, allow_rotation=True)
        assert_valid_packing(self, res, 1000)

    def test_readme_tables_match_benchmark_output(self):
        """README 表格的每一行都必须与 benchmark.py 的真实输出对得上。"""
        benchmark = importlib.import_module("benchmark")
        rows, records = benchmark.check_readme()
        # 每个数据集的旋转关闭/开启两档都不能漏（细长条正是在旋转开启时收益最小）
        self.assertEqual(len(rows), 7)
        self.assertEqual(
            [(r.name, r.rotation) for r in rows],
            [("均匀小矩形", False), ("均匀小矩形", True),
             ("大小混合", False), ("大小混合", True),
             ("细长条", False), ("细长条", True),
             ("均匀中方块", True)])
        gains = [r.gain_h for r in rows]
        self.assertAlmostEqual(min(gains), 28.3, places=1)
        self.assertAlmostEqual(max(gains), 36.5, places=1)
        self.assertEqual(min(rows, key=lambda r: r.gain_h).name, "细长条")
        self.assertTrue(min(rows, key=lambda r: r.gain_h).rotation)
        self.assertEqual(len(records), 4)


if __name__ == "__main__":
    unittest.main()
