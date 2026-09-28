"""pathgeom 自测：解析正确性、包围盒不低估、长度误差界、退化输入。"""

import math
import unittest

from pathgeom import (Move, Line, Quad, Cubic, Path, parse_path,
                      path_bbox, path_length, path_length_error_bound)


def kinds(segs):
    return [s.kind for s in segs]


class TestParse(unittest.TestCase):
    def test_basic_absolute(self):
        segs = parse_path("M 0 0 L 10 0 L 10 10 Z")
        self.assertEqual(kinds(segs), ["M", "L", "L", "L"])
        self.assertEqual(segs[-1].p1, 0j)  # Z 回到起点

    def test_implicit_lineto_after_moveto(self):
        segs = parse_path("M 0 0 10 0 10 10")
        self.assertEqual(kinds(segs), ["M", "L", "L"])
        self.assertEqual(segs[2].p1, 10 + 10j)

    def test_relative_accumulation(self):
        # 连续相对指令坐标必须累加：m 10 10 后两对隐式相对直线。
        segs = parse_path("m 10 10 5 0 5 0")
        self.assertEqual(kinds(segs), ["M", "L", "L"])
        self.assertEqual(segs[0].p0, 10 + 10j)
        self.assertEqual(segs[1].p1, 15 + 10j)
        self.assertEqual(segs[2].p1, 20 + 10j)

    def test_relative_curves_accumulate(self):
        segs = parse_path("M 0 0 c 1 0 1 1 0 1 1 0 1 1 0 1 q 1 1 2 0 t 2 0")
        c1, c2 = segs[1], segs[2]
        self.assertIsInstance(c1, Cubic)
        self.assertEqual(c1.p3, 0 + 1j)
        self.assertEqual(c2.p0, 0 + 1j)          # 段首尾相接
        self.assertEqual(c2.p3, 0 + 2j)          # 相对坐标累加
        q, t = segs[3], segs[4]
        self.assertEqual(q.p2, 2 + 2j)
        self.assertIsInstance(t, Quad)
        self.assertEqual(t.p2, 4 + 2j)
        # T 反射 Q 的控制点：q 控制点 (1,3) -> 反射到 (3,3)
        self.assertEqual(t.p1, 2 * q.p2 - q.p1)

    def test_smooth_cubic_reflection(self):
        segs = parse_path("M 0 0 C 1 0 1 1 2 1 S 3 2 4 2")
        s = segs[2]
        self.assertEqual(s.p1, 2 * (2 + 1j) - (1 + 1j))  # 反射上一控制点

    def test_hv_and_compact_number_syntax(self):
        segs = parse_path("M0 0h10.5.5v-5e0")
        self.assertEqual(kinds(segs), ["M", "L", "L", "L"])
        self.assertEqual(segs[1].p1, 10.5 + 0j)
        self.assertEqual(segs[2].p1, 11.0 + 0j)
        self.assertEqual(segs[3].p1, 11.0 - 5j)

    def test_comma_and_garbage(self):
        self.assertEqual(len(parse_path("M0,0 L1,1")), 2)
        with self.assertRaises(ValueError):
            parse_path("M 0 0 x 1 1")
        with self.assertRaises(ValueError):
            parse_path("1 2 3")
        with self.assertRaises(ValueError):
            parse_path("M 0 0 A 1 1 0 0 1 2 2")  # 圆弧不支持，明确报错

    def test_command_missing_args_is_error(self):
        # 命令缺参数：L 后一个数都没有就跟了下一条命令 H。修复前内层取数循环
        # 一次不转，L 被静默丢弃，包围盒少算一段。
        with self.assertRaisesRegex(ValueError, r"'L'.*expects 2 numbers"):
            parse_path("M 0 0 L H 5")
        # 命令给了一部分参数（串尾只剩 1 个数）同样必须明确报错。
        with self.assertRaisesRegex(ValueError, r"'L'.*expects 2 numbers"):
            parse_path("M 0 0 L 10 10 5")

    def test_command_no_args_is_error(self):
        # 命令无参数：命令后什么参数都没有（串尾）。修复前静默返回空段/部分段。
        with self.assertRaisesRegex(ValueError, r"'L'.*expects 2 numbers"):
            parse_path("M 0 0 L")
        with self.assertRaisesRegex(ValueError, r"'M'.*expects 2 numbers"):
            parse_path("M")


class TestBBox(unittest.TestCase):
    def test_quad_exact_extremum(self):
        # 抛物线顶点在 t=0.5，y = 50。
        q = Quad(0j, 0 + 100j, 100 + 0j)
        self.assertEqual(q.bbox(), (0.0, 0.0, 100.0, 50.0))

    def test_cubic_exact_extremum(self):
        # 构造已知极值的三次曲线：B(t) = (t, 3t(1-t)) 的贝塞尔形式。
        c = Cubic(0j, complex(1 / 3, 1), complex(2 / 3, 1), 1 + 0j)
        x0, y0, x1, y1 = c.bbox()
        self.assertAlmostEqual(y1, 0.75, places=12)  # t=0.5 时 y=0.75
        self.assertAlmostEqual(y0, 0.0, places=12)

    def test_control_bbox_contains_exact(self):
        c = Cubic(0j, 50 + 200j, 150 - 100j, 200 + 0j)
        ex, cx = c.bbox(), c.control_bbox()
        self.assertTrue(cx[0] <= ex[0] and cx[1] <= ex[1]
                        and cx[2] >= ex[2] and cx[3] >= ex[3])

    def test_sampling_stays_inside_bbox(self):
        # 对一批曲线段密集采样，任何采样点不得落在包围盒外。
        segs = parse_path(
            "M 3 4 Q 50 120 100 0 T 200 50 "
            "C 0 200 250 -100 300 50 S 350 300 400 0 L 10 10 Z")
        eps = 1e-9
        for s in segs:
            if isinstance(s, Move):
                continue
            x0, y0, x1, y1 = s.bbox()
            for k in range(1001):
                p = s.point_at(k / 1000)
                self.assertGreaterEqual(p.real, x0 - eps)
                self.assertGreaterEqual(p.imag, y0 - eps)
                self.assertLessEqual(p.real, x1 + eps)
                self.assertLessEqual(p.imag, y1 + eps)


class TestLength(unittest.TestCase):
    def test_line_exact(self):
        line = Line(0j, 3 + 4j)
        self.assertEqual(line.length(1), 5.0)
        self.assertEqual(line.length_error_bound(1), 0.0)

    def test_straight_line_path(self):
        p = Path("M 0 0 L 30 40")
        self.assertEqual(p.length(), 50.0)
        self.assertEqual(p.length_error_bound(), 0.0)

    def test_arc_error_within_bound(self):
        # 四分之一圆弧的三次贝塞尔近似（kappa = 4/3*tan(pi/8)）。
        r = 100.0
        k = 4.0 / 3.0 * math.tan(math.pi / 8.0)
        c = Cubic(complex(r, 0), complex(r, k * r), complex(k * r, r), complex(0, r))
        # 用极细细分的长度作为“真实值”。
        ref = c.length(1 << 18)
        for n in (4, 8, 16, 64, 256):
            err = abs(c.length(n) - ref)
            bound = c.length_error_bound(n)
            self.assertLessEqual(err, bound + 1e-9,
                                 "n=%d err=%g bound=%g" % (n, err, bound))
        # 贝塞尔近似本身与真圆弧长度 r*pi/2 的偏差约 1.4e-4（相对），
        # 这是 kappa 近似的固有误差，与我们的细分误差无关。
        self.assertAlmostEqual(ref, r * math.pi / 2, delta=0.03)

    def test_quad_error_within_bound(self):
        q = Quad(0j, 50 + 100j, 100 + 0j)
        ref = q.length(1 << 18)
        for n in (2, 8, 32, 128):
            self.assertLessEqual(abs(q.length(n) - ref),
                                 q.length_error_bound(n) + 1e-9)

    def test_bound_decreases_with_n(self):
        c = Cubic(0j, 100 + 200j, 300 - 100j, 400 + 0j)
        b1, b2 = c.length_error_bound(8), c.length_error_bound(64)
        self.assertAlmostEqual(b1 / b2, 8.0)


class TestDegenerate(unittest.TestCase):
    def test_empty_path(self):
        segs = parse_path("")
        self.assertEqual(segs, [])
        self.assertIsNone(path_bbox(segs))
        self.assertEqual(path_length(segs), 0.0)
        self.assertEqual(path_length_error_bound(segs), 0.0)

    def test_move_only(self):
        segs = parse_path("M 5 5 M 10 20")
        self.assertIsNone(path_bbox(segs))   # 无绘制段：确定结果为 None
        self.assertEqual(path_length(segs), 0.0)

    def test_zero_length_segments(self):
        segs = parse_path("M 3 3 L 3 3 L 3 3 Z")
        self.assertEqual(path_bbox(segs), (3.0, 3.0, 3.0, 3.0))
        self.assertEqual(path_length(segs), 0.0)

    def test_repeated_points_curve(self):
        c = Cubic(7 + 7j, 7 + 7j, 7 + 7j, 7 + 7j)
        self.assertEqual(c.bbox(), (7.0, 7.0, 7.0, 7.0))
        self.assertEqual(c.length(64), 0.0)
        self.assertEqual(c.length_error_bound(64), 0.0)

    def test_relative_translation_invariance(self):
        # 同一形状的相对坐标版本整体平移 (100,50)：长度不变，包围盒平移。
        abs_path = Path("M 0 0 L 10 0 C 10 10 20 10 20 0 Q 25 -5 30 0 Z")
        rel_path = Path("m 100 50 l 10 0 c 0 10 10 10 10 0 q 5 -5 10 0 z")
        la, lr = abs_path.length(64), rel_path.length(64)
        self.assertAlmostEqual(la, lr, places=9)
        ba, br = abs_path.bbox(), rel_path.bbox()
        self.assertAlmostEqual(br[0] - ba[0], 100.0, places=9)
        self.assertAlmostEqual(br[1] - ba[1], 50.0, places=9)
        self.assertAlmostEqual(br[2] - ba[2], 100.0, places=9)
        self.assertAlmostEqual(br[3] - ba[3], 50.0, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
