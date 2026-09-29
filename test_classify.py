"""分类用例集：IntersectionKind 五类与退化输入的明确分类（D1-D4）。

每个用例同时校验：类别、参数区间 [t0, t1]、端点坐标、以及
"参数区间可复算"（q == p1 + t*(p2-p1) 且 t 可由 q 反投影还原）。
"""

import unittest

import rectclip as rc

K = rc.IntersectionKind
R = (0, 0, 2, 2)


class ClassifySegRectTests(unittest.TestCase):
    def _check(self, p1, p2, rect, kind, t0=None, t1=None, q0=None, q1=None):
        res = rc.classify_segment_rect(p1, p2, rect)
        self.assertEqual(res.kind, kind, res.reason)
        self.assertTrue(res.reason)  # 必须带可解释依据
        # 与旧布尔 API 一致
        self.assertEqual(rc.segment_rect_intersects(p1, p2, rect),
                         kind is not K.DISJOINT)
        if kind is K.DISJOINT:
            self.assertIsNone(res.t0)
            return res
        self.assertIsNotNone(res.t0)
        self.assertLessEqual(res.t0, res.t1)          # 参数顺序保留
        self.assertGreaterEqual(res.t0, 0.0)
        self.assertLessEqual(res.t1, 1.0)
        if t0 is not None:
            self.assertAlmostEqual(res.t0, t0)
        if t1 is not None:
            self.assertAlmostEqual(res.t1, t1)
        if q0 is not None:
            self.assertAlmostEqual(res.q0[0], q0[0])
            self.assertAlmostEqual(res.q0[1], q0[1])
        if q1 is not None:
            self.assertAlmostEqual(res.q1[0], q1[0])
            self.assertAlmostEqual(res.q1[1], q1[1])
        # 参数区间可复算：q = p(t)，且 t 可由 q 反投影还原
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        for t, q in ((res.t0, res.q0), (res.t1, res.q1)):
            self.assertAlmostEqual(q[0], p1[0] + t * dx)
            self.assertAlmostEqual(q[1], p1[1] + t * dy)
        if dx != 0.0 or dy != 0.0:
            den = dx * dx + dy * dy
            for t, q in ((res.t0, res.q0), (res.t1, res.q1)):
                t_back = ((q[0] - p1[0]) * dx + (q[1] - p1[1]) * dy) / den
                self.assertAlmostEqual(t_back, t)
        # 与裁剪结果一致（同一参数区间、同一方向）
        clip = rc.clip_segment_to_rect(p1, p2, rect)
        self.assertEqual((res.t0, res.t1, res.q0, res.q1), clip)
        return res

    def test_proper_crossing(self):
        self._check((-1, 1), (3, 1), R, K.PROPER,
                    t0=0.25, t1=0.75, q0=(0, 1), q1=(2, 1))

    def test_proper_fully_inside(self):
        self._check((0.5, 0.5), (1.5, 1.0), R, K.PROPER, t0=0.0, t1=1.0)

    def test_proper_reversed_direction_preserved(self):
        res = self._check((3, 1), (-1, 1), R, K.PROPER,
                          t0=0.25, t1=0.75, q0=(2, 1), q1=(0, 1))
        self.assertGreater(res.q0[0], res.q1[0])  # 方向从右到左保留

    def test_collinear_overlap(self):  # R3
        self._check((-1, 0), (1, 0), R, K.COLLINEAR_OVERLAP,
                    t0=0.5, t1=1.0, q0=(0, 0), q1=(1, 0))
        self._check((-1, 2), (3, 2), R, K.COLLINEAR_OVERLAP)  # 整条边

    def test_endpoint_touch(self):  # R4
        self._check((2, 2), (5, 5), R, K.ENDPOINT_TOUCH,
                    t0=0.0, t1=0.0, q0=(2, 2), q1=(2, 2))
        self._check((3, 1), (2, 1), R, K.ENDPOINT_TOUCH, t0=1.0, t1=1.0)

    def test_tangent_at_corner(self):  # R5
        self._check((1, 3), (3, 1), R, K.TANGENT,
                    t0=0.5, t1=0.5, q0=(2, 2), q1=(2, 2))

    def test_disjoint(self):
        res = self._check((5, 5), (6, 6), R, K.DISJOINT)
        self.assertIsNone(rc.clip_segment_to_rect((5, 5), (6, 6), R))
        self.assertEqual(res.q0, None)

    def test_degenerate_zero_length_segment(self):  # D1
        self._check((1, 1), (1, 1), R, K.PROPER)            # 点在内部
        self._check((0, 2), (0, 2), R, K.ENDPOINT_TOUCH)    # 点在角点
        self._check((2, 1), (2, 1), R, K.ENDPOINT_TOUCH)    # 点在边上
        self._check((9, 9), (9, 9), R, K.DISJOINT)

    def test_degenerate_point_rect(self):  # D2
        pr = (5, 5, 5, 5)
        self._check((0, 0), (10, 10), pr, K.TANGENT,
                    t0=0.5, t1=0.5, q0=(5, 5), q1=(5, 5))   # 内部命中
        self._check((5, 5), (9, 9), pr, K.ENDPOINT_TOUCH)   # 端点命中
        self._check((6, 0), (6, 10), pr, K.DISJOINT)

    def test_degenerate_line_rect(self):  # D3
        lr = (0, 2, 4, 2)
        self._check((1, 2), (3, 2), lr, K.COLLINEAR_OVERLAP)   # 共线重叠
        self._check((2, 0), (2, 4), lr, K.TANGENT,
                    t0=0.5, t1=0.5)                            # 横穿单点
        self._check((0, 2), (0, 5), lr, K.ENDPOINT_TOUCH)      # 端点命中
        self._check((1, 0), (3, 0), lr, K.DISJOINT)


class ClassifySegSegTests(unittest.TestCase):
    def _check(self, p1, p2, p3, p4, kind):
        res = rc.classify_segments(p1, p2, p3, p4)
        self.assertEqual(res.kind, kind, res.reason)
        self.assertTrue(res.reason)
        self.assertEqual(rc.segments_intersect(p1, p2, p3, p4),
                         kind is not K.DISJOINT)
        if kind is not K.DISJOINT:
            self.assertLessEqual(res.t0, res.t1)
            self.assertLessEqual(res.u0, res.u1)
            # 双侧参数区间可复算：交点坐标一致
            q_t0 = (p1[0] + res.t0 * (p2[0] - p1[0]),
                    p1[1] + res.t0 * (p2[1] - p1[1]))
            q_u0 = (p3[0] + res.u0 * (p4[0] - p3[0]),
                    p3[1] + res.u0 * (p4[1] - p3[1]))
            self.assertAlmostEqual(q_t0[0], q_u0[0])
            self.assertAlmostEqual(q_t0[1], q_u0[1])
        return res

    def test_proper_crossing(self):
        res = self._check((0, 0), (2, 2), (0, 2), (2, 0), K.PROPER)
        self.assertAlmostEqual(res.t0, 0.5)
        self.assertAlmostEqual(res.u0, 0.5)

    def test_endpoint_touch(self):
        self._check((0, 0), (1, 0), (1, 0), (1, 1), K.ENDPOINT_TOUCH)  # 端点-端点
        self._check((0, 0), (2, 0), (1, 0), (1, 3), K.ENDPOINT_TOUCH)  # T 型

    def test_collinear_overlap(self):  # R3
        res = self._check((0, 0), (3, 0), (2, 0), (5, 0), K.COLLINEAR_OVERLAP)
        self.assertAlmostEqual(res.t0, 2 / 3)
        self.assertAlmostEqual(res.t1, 1.0)
        self.assertAlmostEqual(res.u0, 0.0)
        self.assertAlmostEqual(res.u1, 1 / 3)
        # 反向线段：参数顺序仍各自升序、方向各自保留
        res = self._check((3, 0), (0, 0), (5, 0), (2, 0), K.COLLINEAR_OVERLAP)
        self.assertAlmostEqual(res.t0, 0.0)
        self.assertAlmostEqual(res.t1, 1 / 3)

    def test_collinear_endpoint_only(self):
        self._check((0, 0), (3, 0), (3, 0), (5, 0), K.ENDPOINT_TOUCH)

    def test_disjoint(self):
        self._check((0, 0), (1, 1), (2, 2), (3, 3), K.DISJOINT)
        self._check((0, 0), (3, 0), (4, 0), (5, 0), K.DISJOINT)  # 共线不重叠

    def test_degenerate_zero_length(self):  # D4 / R1
        self._check((1, 1), (1, 1), (0, 0), (2, 2), K.ENDPOINT_TOUCH)
        self._check((3, 3), (3, 3), (3, 3), (3, 3), K.ENDPOINT_TOUCH)
        self._check((5, 5), (5, 5), (0, 0), (2, 2), K.DISJOINT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
