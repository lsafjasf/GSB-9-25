"""退化用例集：显式覆盖 docstring 中的规则 R1-R5 与方向/参数顺序。"""

import math
import unittest

import rectclip as rc


class PointRectTests(unittest.TestCase):
    def test_inside_boundary_outside(self):
        r = (0, 0, 2, 2)
        self.assertEqual(rc.point_rect_relation(1, 1, r), rc.PointRectRelation.INSIDE)
        self.assertEqual(rc.point_rect_relation(0, 1, r), rc.PointRectRelation.BOUNDARY)
        self.assertEqual(rc.point_rect_relation(2, 2, r), rc.PointRectRelation.BOUNDARY)  # R4 角点
        self.assertEqual(rc.point_rect_relation(-0.5, 1, r), rc.PointRectRelation.OUTSIDE)

    def test_corner_is_boundary_all_four_corners(self):
        r = (-1, -1, 1, 1)
        for x, y in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
            self.assertEqual(rc.point_rect_relation(x, y, r),
                             rc.PointRectRelation.BOUNDARY)

    def test_point_rect(self):  # R2 点矩形：所有点只可能 BOUNDARY/OUTSIDE
        r = (3, 4, 3, 4)
        self.assertEqual(rc.point_rect_relation(3, 4, r), rc.PointRectRelation.BOUNDARY)
        self.assertEqual(rc.point_rect_relation(3.0 + 1e-10, 4, r),
                         rc.PointRectRelation.BOUNDARY)   # eps 内
        self.assertEqual(rc.point_rect_relation(3.01, 4, r), rc.PointRectRelation.OUTSIDE)

    def test_line_rect(self):  # R2 线矩形
        r = (0, 1, 2, 1)
        self.assertEqual(rc.point_rect_relation(1, 1, r), rc.PointRectRelation.BOUNDARY)
        self.assertEqual(rc.point_rect_relation(1, 0, r), rc.PointRectRelation.OUTSIDE)

    def test_normalized_on_input(self):
        self.assertEqual(rc.point_rect_relation(0.5, 0.5, (2, 2, 0, 0)),
                         rc.PointRectRelation.INSIDE)


class SegmentSegmentTests(unittest.TestCase):
    def test_basic_cross_and_disjoint(self):
        self.assertTrue(rc.segments_intersect((0, 0), (2, 2), (0, 2), (2, 0)))
        self.assertFalse(rc.segments_intersect((0, 0), (1, 1), (2, 2), (3, 3)))

    def test_zero_length_segment_is_a_point(self):  # R1
        self.assertTrue(rc.segments_intersect((1, 1), (1, 1), (0, 0), (2, 2)))
        self.assertTrue(rc.segments_intersect((0, 0), (2, 2), (1, 1), (1, 1)))
        self.assertFalse(rc.segments_intersect((5, 5), (5, 5), (0, 0), (2, 2)))
        self.assertTrue(rc.segments_intersect((3, 3), (3, 3), (3, 3), (3, 3)))

    def test_collinear_overlap(self):  # R3
        self.assertTrue(rc.segments_intersect((0, 0), (3, 0), (2, 0), (5, 0)))
        self.assertTrue(rc.segments_intersect((0, 0), (3, 0), (3, 0), (5, 0)))  # 端点相接
        self.assertFalse(rc.segments_intersect((0, 0), (3, 0), (4, 0), (5, 0)))
        self.assertTrue(rc.segments_intersect((0, 0), (3, 0), (1, 0), (2, 0)))   # 内含

    def test_collinear_touching_corner(self):  # R4
        self.assertTrue(rc.segments_intersect((0, 0), (1, 0), (0, 0), (0, 1)))

    def test_tangency_and_near_miss(self):  # R5 + eps 闭规则
        self.assertTrue(rc.segments_intersect((0, 1), (2, 1), (1, 1), (1, 3)))  # T 型
        self.assertTrue(rc.segments_intersect((0, 1e-10), (2, 1e-10),
                                              (0, 0), (2, 0)))                   # eps 内算相交
        self.assertFalse(rc.segments_intersect((0, 1e-3), (2, 1e-3),
                                               (0, 0), (2, 0)))                  # 超出 eps


class SegmentRectTests(unittest.TestCase):
    R = (0, 0, 2, 2)

    def test_through_inside(self):
        self.assertTrue(rc.segment_rect_intersects((-1, 1), (3, 1), self.R))
        self.assertTrue(rc.segment_rect_intersects((0.5, 0.5), (1.5, 1.5), self.R))

    def test_misses(self):
        self.assertFalse(rc.segment_rect_intersects((-1, 3), (3, 5), self.R))
        self.assertFalse(rc.segment_rect_intersects((-1, -1), (-0.5, -0.5), self.R))

    def test_tangent_at_edge_and_corner(self):  # R4/R5
        self.assertTrue(rc.segment_rect_intersects((-1, 2), (3, 2), self.R))     # 沿边
        self.assertTrue(rc.segment_rect_intersects((-1, 3), (3, -1), self.R))    # 穿过角点
        self.assertTrue(rc.segment_rect_intersects((2, 2), (5, 5), self.R))      # 端点在角点

    def test_collinear_with_edge(self):  # R3
        self.assertTrue(rc.segment_rect_intersects((-1, 0), (1, 0), self.R))
        self.assertFalse(rc.segment_rect_intersects((3, 0), (5, 0), self.R))     # 共线但不重叠

    def test_zero_length_segment(self):  # R1
        self.assertTrue(rc.segment_rect_intersects((1, 1), (1, 1), self.R))
        self.assertTrue(rc.segment_rect_intersects((0, 2), (0, 2), self.R))      # 角点
        self.assertFalse(rc.segment_rect_intersects((3.01, 3.01), (3.01, 3.01), self.R))

    def test_point_rect(self):  # R2
        pr = (5, 5, 5, 5)
        self.assertTrue(rc.segment_rect_intersects((5, 0), (5, 10), pr))
        self.assertTrue(rc.segment_rect_intersects((0, 0), (10, 10), pr))
        self.assertTrue(rc.segment_rect_intersects((5, 5), (5, 5), pr))
        self.assertFalse(rc.segment_rect_intersects((6, 0), (6, 10), pr))

    def test_line_rect(self):  # R2
        lr = (0, 2, 4, 2)
        self.assertTrue(rc.segment_rect_intersects((1, 2), (3, 2), lr))   # 共线重叠
        self.assertTrue(rc.segment_rect_intersects((2, 0), (2, 4), lr))   # 穿过
        self.assertFalse(rc.segment_rect_intersects((1, 0), (3, 0), lr))


class ClipTests(unittest.TestCase):
    R = (0, 0, 2, 2)

    def _assert_on_rect(self, q, r):
        xmin, ymin, xmax, ymax = r
        x, y = q
        self.assertTrue(xmin - 1e-9 <= x <= xmax + 1e-9 and
                        ymin - 1e-9 <= y <= ymax + 1e-9)

    def test_partial_clip_preserves_direction(self):
        res = rc.clip_segment_to_rect((-2, 1), (4, 1), self.R)
        t0, t1, q0, q1 = res
        self.assertAlmostEqual(t0, 2 / 6)
        self.assertAlmostEqual(t1, 4 / 6)
        self.assertTrue(t0 <= t1)
        self.assertAlmostEqual(q0[0], 0.0)
        self.assertAlmostEqual(q1[0], 2.0)
        self._assert_on_rect(q0, self.R)
        self._assert_on_rect(q1, self.R)

    def test_reversed_input_keeps_direction(self):  # 裁剪结果方向必须跟随原线段
        res = rc.clip_segment_to_rect((4, 1), (-2, 1), self.R)
        t0, t1, q0, q1 = res
        self.assertAlmostEqual(q0[0], 2.0)
        self.assertAlmostEqual(q1[0], 0.0)
        self.assertGreater(q0[0], q1[0])  # 方向保留：从右到左

    def test_fully_inside(self):
        res = rc.clip_segment_to_rect((0.5, 0.5), (1.5, 1.0), self.R)
        t0, t1, q0, q1 = res
        self.assertEqual((t0, t1), (0.0, 1.0))
        self.assertEqual(q0, (0.5, 0.5))
        self.assertEqual(q1, (1.5, 1.0))

    def test_outside_returns_none(self):
        self.assertIsNone(rc.clip_segment_to_rect((5, 5), (6, 6), self.R))

    def test_tangent_returns_point(self):  # R5: y=-x+4 只在角点 (2,2) 接触矩形
        res = rc.clip_segment_to_rect((1, 3), (3, 1), self.R)
        self.assertIsNotNone(res)
        t0, t1, q0, q1 = res
        self.assertAlmostEqual(t0, t1)
        self.assertAlmostEqual(q0[0], 2.0)
        self.assertAlmostEqual(q0[1], 2.0)
        self.assertAlmostEqual(q0[0], q1[0])
        self.assertAlmostEqual(q0[1], q1[1])

    def test_zero_length_input(self):  # R1
        self.assertIsNone(rc.clip_segment_to_rect((9, 9), (9, 9), self.R))
        res = rc.clip_segment_to_rect((1, 1), (1, 1), self.R)
        self.assertEqual(res[2], res[3])
        self.assertEqual(res[2], (1, 1))

    def test_point_rect(self):  # R2
        pr = (1, 1, 1, 1)
        res = rc.clip_segment_to_rect((0, 0), (2, 2), pr)
        self.assertIsNotNone(res)
        t0, t1, q0, q1 = res
        self.assertAlmostEqual(t0, t1)
        self.assertTrue(math.isclose(q0[0], 1.0, abs_tol=1e-9))
        self.assertTrue(math.isclose(q0[1], 1.0, abs_tol=1e-9))
        self.assertIsNone(rc.clip_segment_to_rect((2, 0), (4, 0), pr))

    def test_line_rect_overlap(self):  # R3 与退化边共线重叠
        lr = (0, 1, 2, 1)
        res = rc.clip_segment_to_rect((-1, 1), (3, 1), lr)
        t0, t1, q0, q1 = res
        self.assertAlmostEqual(q0[0], 0.0)
        self.assertAlmostEqual(q1[0], 2.0)
        self.assertTrue(t0 <= t1)

    def test_param_consistency(self):  # q = p1 + t*(p2-p1) 恒成立
        p1, p2 = (-3.0, -2.0), (5.0, 4.0)
        t0, t1, q0, q1 = rc.clip_segment_to_rect(p1, p2, self.R)
        for t, q in ((t0, q0), (t1, q1)):
            self.assertAlmostEqual(q[0], p1[0] + t * (p2[0] - p1[0]))
            self.assertAlmostEqual(q[1], p1[1] + t * (p2[1] - p1[1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
