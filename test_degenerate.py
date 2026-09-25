"""退化情形用例集：零长度线段、点矩形、共线重叠、角点相切。"""
import unittest

from rect_seg import (
    INSIDE, BOUNDARY, OUTSIDE,
    point_rect_relation, point_on_segment, segments_intersect,
    seg_rect_intersect, clip_segment_to_rect,
)

RECT = (0.0, 0.0, 10.0, 10.0)


class TestPointRect(unittest.TestCase):
    def test_inside(self):
        self.assertEqual(point_rect_relation(5, 5, RECT), INSIDE)

    def test_boundary_edges_and_corner(self):
        self.assertEqual(point_rect_relation(0, 5, RECT), BOUNDARY)
        self.assertEqual(point_rect_relation(10, 10, RECT), BOUNDARY)  # 角

    def test_outside(self):
        self.assertEqual(point_rect_relation(-1, 5, RECT), OUTSIDE)
        self.assertEqual(point_rect_relation(11, 11, RECT), OUTSIDE)

    def test_degenerate_rect(self):
        # 点矩形：边界即点本身
        self.assertEqual(point_rect_relation(3, 4, (3, 4, 3, 4)), BOUNDARY)
        self.assertEqual(point_rect_relation(3, 5, (3, 4, 3, 4)), OUTSIDE)
        # 零宽矩形：线段上的点为边界
        self.assertEqual(point_rect_relation(3, 5, (3, 0, 3, 10)), BOUNDARY)


class TestSegSeg(unittest.TestCase):
    def test_cross(self):
        self.assertTrue(segments_intersect(((0, 0), (10, 10)), ((0, 10), (10, 0))))

    def test_disjoint(self):
        self.assertFalse(segments_intersect(((0, 0), (1, 1)), ((2, 2), (3, 0))))

    def test_collinear_overlap(self):
        self.assertTrue(segments_intersect(((0, 0), (10, 0)), ((5, 0), (15, 0))))

    def test_collinear_touch_endpoint(self):
        self.assertTrue(segments_intersect(((0, 0), (5, 0)), ((5, 0), (9, 0))))

    def test_collinear_disjoint(self):
        self.assertFalse(segments_intersect(((0, 0), (4, 0)), ((5, 0), (9, 0))))

    def test_endpoint_touch(self):
        self.assertTrue(segments_intersect(((0, 0), (5, 5)), ((5, 5), (9, 0))))

    def test_zero_length(self):
        self.assertTrue(segments_intersect(((3, 3), (3, 3)), ((0, 0), (9, 9))))
        self.assertFalse(segments_intersect(((3, 4), (3, 4)), ((0, 0), (9, 9))))
        self.assertTrue(segments_intersect(((3, 3), (3, 3)), ((3, 3), (3, 3))))
        self.assertFalse(segments_intersect(((3, 3), (3, 3)), ((3, 4), (3, 4))))


class TestSegRect(unittest.TestCase):
    def test_through(self):
        self.assertTrue(seg_rect_intersect((-5, 5), (15, 5), RECT))

    def test_inside(self):
        self.assertTrue(seg_rect_intersect((2, 2), (8, 8), RECT))

    def test_outside(self):
        self.assertFalse(seg_rect_intersect((-5, -5), (-1, -1), RECT))
        self.assertFalse(seg_rect_intersect((-5, 20), (15, 20), RECT))

    def test_zero_length_segment(self):
        self.assertTrue(seg_rect_intersect((5, 5), (5, 5), RECT))       # 内部
        self.assertTrue(seg_rect_intersect((0, 5), (0, 5), RECT))       # 边界
        self.assertFalse(seg_rect_intersect((20, 5), (20, 5), RECT))    # 外部

    def test_point_rect(self):
        pr = (3, 3, 3, 3)
        self.assertTrue(seg_rect_intersect((0, 0), (9, 9), pr))
        self.assertFalse(seg_rect_intersect((0, 0), (9, 8), pr))
        self.assertTrue(seg_rect_intersect((3, 3), (3, 3), pr))

    def test_zero_width_rect(self):
        zr = (5, 0, 5, 10)
        self.assertTrue(seg_rect_intersect((0, 5), (10, 5), zr))
        self.assertFalse(seg_rect_intersect((0, 5), (4, 5), zr))

    def test_collinear_edge_overlap(self):
        # 与底边共线重叠
        self.assertTrue(seg_rect_intersect((2, 0), (8, 0), RECT))
        self.assertTrue(seg_rect_intersect((-5, 0), (15, 0), RECT))
        # 共线但在边的延长线上
        self.assertFalse(seg_rect_intersect((-9, 0), (-1, 0), RECT))

    def test_corner_touch(self):
        # 端点恰好落在角上
        self.assertTrue(seg_rect_intersect((-5, -5), (0, 0), RECT))
        self.assertTrue(seg_rect_intersect((10, 10), (20, 20), RECT))
        # 穿过角的对角线
        self.assertTrue(seg_rect_intersect((-5, -5), (0, 0), RECT))

    def test_tangent(self):
        # 与边相切（贴边经过）
        self.assertTrue(seg_rect_intersect((-5, 0), (15, 0), RECT))
        # 对角线恰好擦过角 (10,0)：线 y=-x+10
        self.assertTrue(seg_rect_intersect((5, 5), (15, -5), RECT))


class TestClip(unittest.TestCase):
    def test_through(self):
        r = clip_segment_to_rect((-5, 5), (15, 5), RECT)
        self.assertIsNotNone(r)
        (a, b, t0, t1) = r
        self.assertAlmostEqual(a[0], 0.0); self.assertAlmostEqual(a[1], 5.0)
        self.assertAlmostEqual(b[0], 10.0); self.assertAlmostEqual(b[1], 5.0)
        self.assertLessEqual(t0, t1)
        self.assertAlmostEqual(t0, 0.25); self.assertAlmostEqual(t1, 0.75)

    def test_direction_preserved(self):
        # 反向线段：裁剪结果方向应与原方向一致
        r = clip_segment_to_rect((15, 5), (-5, 5), RECT)
        (a, b, t0, t1) = r
        self.assertAlmostEqual(a[0], 10.0)
        self.assertAlmostEqual(b[0], 0.0)
        self.assertGreater(a[0], b[0])  # 方向未翻转

    def test_inside_unchanged(self):
        r = clip_segment_to_rect((2, 2), (8, 8), RECT)
        (a, b, t0, t1) = r
        self.assertEqual((t0, t1), (0.0, 1.0))
        self.assertEqual(a, (2, 2)); self.assertEqual(b, (8, 8))

    def test_outside_none(self):
        self.assertIsNone(clip_segment_to_rect((-5, 20), (15, 20), RECT))
        self.assertIsNone(clip_segment_to_rect((-9, 0), (-1, 0), RECT))

    def test_zero_length(self):
        r = clip_segment_to_rect((5, 5), (5, 5), RECT)
        self.assertIsNotNone(r)
        self.assertEqual(r[0], (5, 5)); self.assertEqual(r[1], (5, 5))
        self.assertIsNone(clip_segment_to_rect((20, 5), (20, 5), RECT))

    def test_collinear_edge(self):
        r = clip_segment_to_rect((-5, 0), (15, 0), RECT)
        (a, b, t0, t1) = r
        self.assertAlmostEqual(a[0], 0.0); self.assertAlmostEqual(b[0], 10.0)
        self.assertAlmostEqual(a[1], 0.0); self.assertAlmostEqual(b[1], 0.0)

    def test_corner_endpoint(self):
        r = clip_segment_to_rect((-5, -5), (0, 0), RECT)
        self.assertIsNotNone(r)
        (a, b, t0, t1) = r
        self.assertAlmostEqual(a[0], 0.0); self.assertAlmostEqual(a[1], 0.0)
        self.assertAlmostEqual(b[0], 0.0); self.assertAlmostEqual(b[1], 0.0)

    def test_degenerate_rect(self):
        # 点矩形：线段过该点 -> 裁剪为该点
        r = clip_segment_to_rect((0, 0), (10, 10), (5, 5, 5, 5))
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r[0][0], 5.0); self.assertAlmostEqual(r[1][0], 5.0)
        self.assertIsNone(clip_segment_to_rect((0, 0), (10, 9), (5, 5, 5, 5)))
        # 零宽矩形：退化为竖直线段
        r = clip_segment_to_rect((0, 5), (10, 5), (5, 0, 5, 10))
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r[0][0], 5.0); self.assertAlmostEqual(r[0][1], 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
