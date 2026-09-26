"""rectpack 约束断言测试。运行: python3 -m unittest test_rectpack -v"""

import random
import unittest

from rectpack import StripPacker, pack


def assert_valid_result(testcase, result, rects, allow_rotate):
    """核心可断言约束：不重叠、在容器内、放置/未放置完整划分、旋转标注正确。"""
    ps = result.placements
    # 1) 每个矩形要么被放置、要么在 unplaced，且恰好一次
    placed_idx = sorted(p.index for p in ps)
    testcase.assertEqual(sorted(placed_idx + result.unplaced),
                         list(range(len(rects))))
    # 2) 均在容器内
    for p in ps:
        testcase.assertGreaterEqual(p.x, 0)
        testcase.assertGreaterEqual(p.y, 0)
        testcase.assertLessEqual(p.x + p.width, result.container_width + 1e-9)
        testcase.assertLessEqual(p.y + p.height, result.height + 1e-9)
        # 3) 旋转标注与尺寸一致
        w0, h0 = rects[p.index]
        if p.rotated:
            testcase.assertTrue(allow_rotate)
            testcase.assertEqual((p.width, p.height), (h0, w0))
        else:
            testcase.assertEqual((p.width, p.height), (w0, h0))
    # 4) 两两不重叠
    for i in range(len(ps)):
        for j in range(i + 1, len(ps)):
            a, b = ps[i], ps[j]
            overlap = not (a.x + a.width <= b.x or b.x + b.width <= a.x or
                           a.y + a.height <= b.y or b.y + b.height <= a.y)
            testcase.assertFalse(overlap, f"矩形 {a.index} 与 {b.index} 重叠")
    # 5) 利用率合法
    testcase.assertGreaterEqual(result.utilization, 0.0)
    testcase.assertLessEqual(result.utilization, 1.0 + 1e-9)


def random_rects(n, lo, hi, seed):
    rng = random.Random(seed)
    return [(rng.randint(lo, hi), rng.randint(lo, hi)) for _ in range(n)]


class TestConstraints(unittest.TestCase):
    def test_random_instances_all_modes(self):
        for seed in range(10):
            rects = random_rects(60, 5, 60, seed)
            for rotate in (False, True):
                for fixed in (False, True):
                    res = pack(100, rects, allow_rotate=rotate, fixed_order=fixed)
                    assert_valid_result(self, res, rects, rotate)

    def test_determinism(self):
        rects = random_rects(200, 3, 50, 42)
        r1 = pack(120, rects, allow_rotate=True)
        r2 = pack(120, rects, allow_rotate=True)
        self.assertEqual(r1.placements, r2.placements)
        self.assertEqual(r1.height, r2.height)

    def test_rotation_marks_rotated(self):
        # 高瘦矩形 + 窄容器：不旋转放不下，旋转后可放
        res = pack(50, [(80, 20)], allow_rotate=True)
        self.assertEqual(len(res.placements), 1)
        self.assertTrue(res.placements[0].rotated)
        self.assertEqual((res.placements[0].width, res.placements[0].height), (20, 80))

    def test_too_wide_rect_is_unplaced_not_dropped(self):
        rects = [(10, 10), (200, 5), (30, 30)]
        res = pack(100, rects, allow_rotate=False)
        self.assertEqual(res.unplaced, [1])
        self.assertEqual(len(res.placements), 2)
        # 旋转后 (200,5)->(5,200) 可放入，应被放置且标注旋转
        res2 = pack(100, rects, allow_rotate=True)
        self.assertEqual(res2.unplaced, [])
        p = next(p for p in res2.placements if p.index == 1)
        self.assertTrue(p.rotated)
        # 两个方向都超宽时，旋转也救不了
        res3 = pack(100, [(200, 150)], allow_rotate=True)
        self.assertEqual(res3.unplaced, [0])

    def test_zero_size_rejected(self):
        with self.assertRaises(ValueError):
            pack(100, [(0, 10)])
        with self.assertRaises(ValueError):
            pack(100, [(10, 0)])
        with self.assertRaises(ValueError):
            pack(100, [(-5, 10)])

    def test_empty_set(self):
        res = pack(100, [])
        self.assertEqual(res.height, 0.0)
        self.assertEqual(res.placements, [])
        self.assertEqual(res.unplaced, [])
        self.assertEqual(res.utilization, 0.0)

    def test_invalid_container_width(self):
        with self.assertRaises(ValueError):
            StripPacker(0)
        with self.assertRaises(ValueError):
            StripPacker(-3)

    def test_fixed_order_preserves_sequence(self):
        # 固定顺序模式下，先到的矩形占用左下角，后续不得抢占其位置
        rects = [(50, 50), (50, 50), (50, 50)]
        res = pack(100, rects, fixed_order=True)
        ys = sorted((p.x, p.y) for p in res.placements)
        self.assertEqual(ys[0], (0.0, 0.0))

    def test_rotation_improves_or_matches(self):
        rects = random_rects(100, 10, 80, 7)
        h_no = pack(100, rects, allow_rotate=False).height
        h_rot = pack(100, rects, allow_rotate=True).height
        self.assertLessEqual(h_rot, h_no + 1e-9)


if __name__ == "__main__":
    unittest.main()
