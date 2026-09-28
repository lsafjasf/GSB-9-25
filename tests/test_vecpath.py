import math
import unittest

from vecpath import (
    Segment, parse, point_at, segment_bbox, path_bbox,
    segment_length, path_length,
    split_segment, point_at_length, clip_path, to_path_data,
)


class TestParse(unittest.TestCase):
    def test_absolute_basic(self):
        segs = parse("M 0 0 L 10 0 L 10 10 Z")
        self.assertEqual([s.kind for s in segs], ["M", "L", "L", "Z"])
        self.assertEqual(segs[3].points, ((10.0, 10.0), (0.0, 0.0)))

    def test_relative_accumulation(self):
        # 连续相对指令坐标必须累加
        segs = parse("m 1 1 l 2 2 l 2 2 l -4 -4")
        pts = [s.points[-1] for s in segs]
        self.assertEqual(pts, [(1, 1), (3, 3), (5, 5), (1, 1)])

    def test_implicit_repetition(self):
        # 一条指令后跟多组参数 = 隐式重复
        segs = parse("M 0 0 L 1 0 2 0 3 0")
        self.assertEqual([s.kind for s in segs], ["M", "L", "L", "L"])
        self.assertEqual(segs[-1].points, ((2.0, 0.0), (3.0, 0.0)))

    def test_moveto_then_implicit_lineto(self):
        segs = parse("M 0 0 1 1 2 2")
        self.assertEqual([s.kind for s in segs], ["M", "L", "L"])
        self.assertEqual(segs[2].points, ((1.0, 1.0), (2.0, 2.0)))

    def test_relative_moveto_then_implicit_lineto(self):
        # 相对 moveto 后续隐式组也是相对 lineto
        segs = parse("m 10 10 5 0 5 0")
        self.assertEqual([s.kind for s in segs], ["M", "L", "L"])
        self.assertEqual(segs[1].points, ((10.0, 10.0), (15.0, 10.0)))
        self.assertEqual(segs[2].points, ((15.0, 10.0), (20.0, 10.0)))

    def test_hv(self):
        segs = parse("M 1 1 H 5 V 5 h -2 v -2")
        self.assertEqual(segs[1].points, ((1, 1), (5, 1)))
        self.assertEqual(segs[2].points, ((5, 1), (5, 5)))
        self.assertEqual(segs[3].points, ((5, 5), (3, 5)))
        self.assertEqual(segs[4].points, ((3, 5), (3, 3)))

    def test_smooth_cubic_reflection(self):
        segs = parse("M 0 0 C 1 0 2 1 3 1 S 5 0 6 0")
        c = segs[2]
        self.assertEqual(c.kind, "C")
        # 上一控制点 (2,1) 关于当前点 (3,1) 的反射 = (4,1)
        self.assertEqual(c.points[1], (4.0, 1.0))
        self.assertEqual(c.points[2], (5.0, 0.0))

    def test_smooth_quad_reflection(self):
        segs = parse("M 0 0 Q 1 2 2 0 T 4 0")
        q = segs[2]
        self.assertEqual(q.kind, "Q")
        # 上一控制点 (1,2) 关于 (2,0) 的反射 = (3,-2)
        self.assertEqual(q.points[1], (3.0, -2.0))

    def test_smooth_without_previous_curve(self):
        # 前一段不是曲线时, 平滑控制点 = 当前点
        segs = parse("M 0 0 L 1 1 S 2 2 3 3")
        self.assertEqual(segs[2].points[1], (1.0, 1.0))

    def test_compact_number_parsing(self):
        # SVG 允许省略分隔符: 符号与小数点即分隔
        segs = parse("M1-2L.5.25e1")
        self.assertEqual(segs[0].points, ((1.0, -2.0),))
        self.assertEqual(segs[1].points, ((1.0, -2.0), (0.5, 2.5)))

    def test_close_resets_subpath_start(self):
        segs = parse("M 1 1 L 2 2 Z L 3 3 Z")
        self.assertEqual(segs[2].points, ((2.0, 2.0), (1.0, 1.0)))
        # Z 之后从子路径起点继续
        self.assertEqual(segs[3].points, ((1.0, 1.0), (3.0, 3.0)))
        self.assertEqual(segs[4].points, ((3.0, 3.0), (1.0, 1.0)))

    def test_arc_rejected(self):
        with self.assertRaises(ValueError):
            parse("M 0 0 A 1 1 0 0 1 2 2")

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            parse("1 2 3")
        with self.assertRaises(ValueError):
            parse("M 0 0 X 5")
        with self.assertRaises(ValueError):
            parse("M")


class TestBBox(unittest.TestCase):
    def test_line_bbox(self):
        segs = parse("M 1 2 L 3 4")
        self.assertEqual(path_bbox(segs), (1.0, 2.0, 3.0, 4.0))

    def test_quad_exact_extremum(self):
        # Q (0,0) (1,2) (2,0): y 在 t=0.5 取最大值 1.0
        segs = parse("M 0 0 Q 1 2 2 0")
        box = path_bbox(segs)
        self.assertEqual(box, (0.0, 0.0, 2.0, 1.0))

    def test_cubic_exact_extremum(self):
        # C (0,0) (0,1) (1,1) (1,0): y 在 t=0.5 取最大值 0.75
        segs = parse("M 0 0 C 0 1 1 1 1 0")
        box = path_bbox(segs)
        self.assertAlmostEqual(box[3], 0.75)
        self.assertEqual(box[0], 0.0)
        self.assertEqual(box[2], 1.0)

    def test_cubic_two_extrema(self):
        # 构造 y 方向有两个极值的三次曲线
        segs = parse("M 0 0 C 0 3 1 -3 1 0")
        box = path_bbox(segs)
        # y(t) = 9t(1-t)(t-0.5)*... 直接验证: 极值点 y = ±1.0 (t = (3±√3)/6)
        ts = [(3 - math.sqrt(3)) / 6, (3 + math.sqrt(3)) / 6]
        ys = sorted(point_at(segs[1], t)[1] for t in ts)
        self.assertAlmostEqual(box[1], ys[0])
        self.assertAlmostEqual(box[3], ys[1])

    def test_bbox_not_underestimate_vs_control_hull(self):
        # 精确盒必须被控制点盒包含 (合理性), 且采样点都在盒内
        segs = parse("M 0 0 C 5 10 15 -10 20 0 Q 25 10 30 0")
        box = path_bbox(segs)
        ctrl = [p for s in segs for p in s.points]
        cx = [p[0] for p in ctrl]
        cy = [p[1] for p in ctrl]
        self.assertGreaterEqual(box[0], min(cx) - 1e-9)
        self.assertGreaterEqual(box[1], min(cy) - 1e-9)
        self.assertLessEqual(box[2], max(cx) + 1e-9)
        self.assertLessEqual(box[3], max(cy) + 1e-9)
        for s in segs:
            if s.kind == "M":
                continue
            for i in range(1001):
                x, y = point_at(s, i / 1000)
                self.assertGreaterEqual(x, box[0] - 1e-9)
                self.assertGreaterEqual(y, box[1] - 1e-9)
                self.assertLessEqual(x, box[2] + 1e-9)
                self.assertLessEqual(y, box[3] + 1e-9)


class TestLength(unittest.TestCase):
    def test_line_exact(self):
        segs = parse("M 0 0 L 3 4")
        length, err = path_length(segs)
        self.assertEqual(length, 5.0)
        self.assertEqual(err, 0.0)

    def test_circle_approximation(self):
        # 16 段三次贝塞尔近似单位圆 (每段 22.5° 圆弧), 真值 2π
        # 该近似本身的几何误差 ~1e-6 量级, 远小于检验阈值
        n = 16
        k = 4.0 / 3.0 * math.tan(math.pi / (2 * n))
        parts = []
        for i in range(n):
            a0 = 2 * math.pi * i / n
            a1 = 2 * math.pi * (i + 1) / n
            am = (a0 + a1) / 2
            x0, y0 = math.cos(a0), math.sin(a0)
            x3, y3 = math.cos(a1), math.sin(a1)
            mx, my = math.cos(am), math.sin(am)
            # 控制点在两端正点切线交点方向上
            c1 = (x0 + k * (-y0), y0 + k * x0)
            c2 = (x3 - k * (-y3), y3 - k * x3)
            parts.append(f"C {c1[0]} {c1[1]} {c2[0]} {c2[1]} {x3} {y3}")
        d = "M 1 0 " + " ".join(parts) + " Z"
        segs = parse(d)
        length, err = path_length(segs, tol=1e-10)
        self.assertAlmostEqual(length, 2 * math.pi, places=5)
        self.assertLess(err, 1e-9)

    def test_error_bound_self_consistent(self):
        # 粗 tol 的估值 ± 报告误差界 必须覆盖 细 tol 的估值
        segs = parse("M 0 0 C 3 7 8 -4 12 5 Q 15 9 20 2 C 25 -5 30 8 35 0")
        coarse, coarse_err = path_length(segs, tol=1e-2)
        fine, fine_err = path_length(segs, tol=1e-10)
        self.assertLessEqual(coarse - coarse_err, fine + fine_err + 1e-12)
        self.assertGreaterEqual(coarse + coarse_err, fine - fine_err - 1e-12)
        self.assertLess(coarse_err, 1e-2 * 3)  # 每段界内

    def test_quad_length_monotonic(self):
        # 更细的 tol 应给出不更差的界
        segs = parse("M 0 0 Q 5 10 10 0")
        _, e1 = path_length(segs, tol=1e-3)
        _, e2 = path_length(segs, tol=1e-6)
        self.assertLess(e2, e1)


class TestDegenerate(unittest.TestCase):
    def test_empty_path(self):
        segs = parse("")
        self.assertEqual(segs, [])
        self.assertIsNone(path_bbox(segs))
        self.assertEqual(path_length(segs), (0.0, 0.0))

    def test_move_only(self):
        segs = parse("M 3 4")
        self.assertEqual(path_bbox(segs), (3.0, 4.0, 3.0, 4.0))
        self.assertEqual(path_length(segs), (0.0, 0.0))

    def test_zero_length_segments(self):
        segs = parse("M 1 1 L 1 1 C 1 1 1 1 1 1 Q 1 1 1 1 Z")
        self.assertEqual(path_bbox(segs), (1.0, 1.0, 1.0, 1.0))
        length, err = path_length(segs)
        self.assertEqual(length, 0.0)
        self.assertEqual(err, 0.0)

    def test_repeated_points(self):
        segs = parse("M 0 0 L 5 0 L 5 0 L 5 0 L 10 0")
        length, _ = path_length(segs)
        self.assertEqual(length, 10.0)
        self.assertEqual(path_bbox(segs), (0.0, 0.0, 10.0, 0.0))

    def test_relative_translation(self):
        # 同一相对路径, 不同起点 => 整体平移, 形状与长度不变
        a = parse("m 0 0 l 3 0 q 2 4 4 0 c 1 -3 3 -3 4 0 z")
        b = parse("m 100 50 l 3 0 q 2 4 4 0 c 1 -3 3 -3 4 0 z")
        ba, bb = path_bbox(a), path_bbox(b)
        self.assertAlmostEqual(bb[0] - ba[0], 100.0)
        self.assertAlmostEqual(bb[1] - ba[1], 50.0)
        self.assertAlmostEqual(bb[2] - ba[2], 100.0)
        self.assertAlmostEqual(bb[3] - ba[3], 50.0)
        la, lb = path_length(a)[0], path_length(b)[0]
        self.assertAlmostEqual(la, lb)


class TestSplit(unittest.TestCase):
    def test_split_line(self):
        (seg,) = parse("M 0 0 L 10 0")[1:]
        left, right = split_segment(seg, 0.3)
        self.assertEqual(left.points, ((0.0, 0.0), (3.0, 0.0)))
        self.assertEqual(right.points, ((3.0, 0.0), (10.0, 0.0)))

    def test_split_point_consistency(self):
        # 分割点必须等于原曲线在该参数处的点
        for d in ("M 1 2 Q 3 8 7 -4", "M 1 2 C 3 8 5 -6 7 4"):
            seg = parse(d)[1]
            for t in (0.0, 0.25, 0.5, 0.9, 1.0):
                left, right = split_segment(seg, t)
                p = point_at(seg, t)
                self.assertAlmostEqual(left.points[-1][0], p[0])
                self.assertAlmostEqual(left.points[-1][1], p[1])
                self.assertAlmostEqual(right.points[0][0], p[0])
                self.assertAlmostEqual(right.points[0][1], p[1])

    def test_split_length_additive(self):
        # 左右两段长度之和 = 原段长度 (精确子段, 不引入近似)
        seg = parse("M 0 0 C 3 7 8 -4 12 5")[1]
        total, terr = segment_length(seg, tol=1e-9)
        left, right = split_segment(seg, 0.4)
        ll, le = segment_length(left, tol=1e-9)
        rl, re = segment_length(right, tol=1e-9)
        self.assertAlmostEqual(ll + rl, total, delta=terr + le + re + 1e-9)

    def test_split_invalid(self):
        with self.assertRaises(ValueError):
            split_segment(parse("M 1 1")[0], 0.5)  # M 不可细分
        with self.assertRaises(ValueError):
            split_segment(parse("M 0 0 L 1 1")[1], 1.5)


class TestClipPath(unittest.TestCase):
    def test_line_clipped(self):
        segs = parse("M -10 0 L 10 0")
        clipped = clip_path(segs, (-5, -5, 5, 5))
        self.assertEqual([s.kind for s in clipped], ["M", "L"])
        self.assertEqual(clipped[0].points, ((-5.0, 0.0),))
        self.assertEqual(clipped[1].points, ((-5.0, 0.0), (5.0, 0.0)))

    def test_fully_inside_unchanged(self):
        segs = parse("M 1 1 L 2 2 Q 3 4 4 2 C 5 0 6 0 7 2")
        clipped = clip_path(segs, (0, 0, 10, 10))
        self.assertEqual(clipped, segs)

    def test_fully_outside_empty(self):
        segs = parse("M 100 100 C 110 120 120 80 130 100")
        self.assertEqual(clip_path(segs, (0, 0, 10, 10)), [])

    def test_curve_bbox_inside_rect(self):
        # 裁剪后包围盒 ⊆ 矩形 ∩ 原包围盒
        segs = parse("M 0 0 C 0 3 1 -3 1 0 S 2 -3 2 0 Q 5 5 8 0 L 12 0 Z")
        rect = (0.5, -1.0, 6.0, 1.5)
        clipped = clip_path(segs, rect)
        box, obox = path_bbox(clipped), path_bbox(segs)
        eps = 1e-9
        self.assertGreaterEqual(box[0], rect[0] - eps)
        self.assertGreaterEqual(box[1], rect[1] - eps)
        self.assertLessEqual(box[2], rect[2] + eps)
        self.assertLessEqual(box[3], rect[3] + eps)
        self.assertGreaterEqual(box[0], obox[0] - eps)
        self.assertGreaterEqual(box[1], obox[1] - eps)
        self.assertLessEqual(box[2], obox[2] + eps)
        self.assertLessEqual(box[3], obox[3] + eps)

    def test_length_not_increased(self):
        segs = parse("M 0 0 C 0 3 1 -3 1 0 S 2 -3 2 0 Q 5 5 8 0 L 12 0 Z")
        clipped = clip_path(segs, (0.5, -1.0, 6.0, 1.5))
        lo, eo = path_length(segs, tol=1e-6)
        lc, ec = path_length(clipped, tol=1e-6)
        self.assertLessEqual(lc, lo + eo + ec)

    def test_roundtrip_reparseable(self):
        # 裁剪结果序列化后必须能被重新解析且完全一致
        segs = parse("m 3 4 c 1 2 3 -4 5 0 q 2 6 4 0 l -1 -1 z")
        clipped = clip_path(segs, (2.5, 3.0, 9.0, 6.5))
        self.assertEqual(parse(to_path_data(clipped)), clipped)

    def test_z_treated_as_line(self):
        # 闭合段按回到起点的直线参与裁剪
        segs = parse("M 0 0 L 4 0 Z")
        clipped = clip_path(segs, (1, -1, 3, 1))
        kinds = [s.kind for s in clipped]
        self.assertNotIn("Z", kinds)
        length, _ = path_length(clipped)
        self.assertAlmostEqual(length, 4.0)  # 去程回程各截出长度 2

    def test_multi_piece_gets_moveto(self):
        # 一条曲线两次穿过矩形 -> 两个片段, 之间必须有 M
        segs = parse("M 0 0 C 3 0 -1 10 2 10")  # 进出再进入
        clipped = clip_path(segs, (0, -1, 1.5, 1))
        kinds = [s.kind for s in clipped]
        self.assertGreaterEqual(kinds.count("M"), 1)
        self.assertEqual(kinds[0], "M")

    def test_invalid_rect(self):
        with self.assertRaises(ValueError):
            clip_path(parse("M 0 0 L 1 1"), (5, 0, 0, 5))


class TestToPathData(unittest.TestCase):
    def test_roundtrip_full_path(self):
        d = "M 0 0 C 0 1 1 1 1 0 Q 2 -1 3 0 Z M 5 5 L 6 6 T 8 8 S 9 9 10 10"
        segs = parse(d)
        self.assertEqual(parse(to_path_data(segs)), segs)

    def test_roundtrip_relative_and_compact(self):
        segs = parse("m1-2l.5.25e1 c 1 2 3 -4 5 0 q 2 6 4 0 z")
        self.assertEqual(parse(to_path_data(segs)), segs)

    def test_empty(self):
        self.assertEqual(to_path_data([]), "")
        self.assertEqual(parse(to_path_data([])), [])


class TestPointAtLength(unittest.TestCase):
    def test_line_exact(self):
        segs = parse("M 0 0 L 10 0 L 10 10")
        pt, err = point_at_length(segs, 5.0)
        self.assertEqual(pt, (5.0, 0.0))
        self.assertEqual(err, 0.0)
        # 跨段定位
        pt, _ = point_at_length(segs, 15.0)
        self.assertEqual(pt, (10.0, 5.0))

    def test_endpoints(self):
        segs = parse("M 1 2 C 3 8 5 -6 7 4")
        total, _ = path_length(segs)
        p0, _ = point_at_length(segs, 0.0)
        p1, _ = point_at_length(segs, total)
        self.assertEqual(p0, (1.0, 2.0))
        self.assertAlmostEqual(p1[0], 7.0)
        self.assertAlmostEqual(p1[1], 4.0)

    def test_out_of_range(self):
        segs = parse("M 0 0 L 3 4")
        with self.assertRaises(ValueError):
            point_at_length(segs, -1.0)
        with self.assertRaises(ValueError):
            point_at_length(segs, 6.0)
        with self.assertRaises(ValueError):
            point_at_length([], 0.0)

    def test_curve_consistency_with_sampling(self):
        # 弧长定位点与密集采样 (弦长累加 + 线性插值) 的参考点一致,
        # 偏差不超过 报告误差界 + 采样间距
        segs = parse("M 0 0 C 3 7 8 -4 12 5 Q 15 9 20 2 C 25 -5 30 8 35 0")
        total, terr = path_length(segs, tol=1e-8)
        n = 4000
        cum, pts, gap = [0.0], [], 0.0
        for seg in segs:
            if seg.kind == "M":
                pts.append(seg.points[0])
                continue
            prev = seg.points[0]
            for i in range(1, n + 1):
                p = point_at(seg, i / n)
                step = math.hypot(p[0] - prev[0], p[1] - prev[1])
                gap = max(gap, step)
                cum.append(cum[-1] + step)
                pts.append(p)
                prev = p
        for k in range(1, 20):
            s = total * k / 20
            pt, err = point_at_length(segs, s, tol=1e-8)
            # 参考点: 采样弦长序列上按弧长插值
            j = 0
            while j + 1 < len(cum) and cum[j + 1] < s:
                j += 1
            span = cum[j + 1] - cum[j]
            r = 0.0 if span == 0.0 else (s - cum[j]) / span
            ref = (pts[j][0] + (pts[j + 1][0] - pts[j][0]) * r,
                   pts[j][1] + (pts[j + 1][1] - pts[j][1]) * r)
            dev = math.hypot(pt[0] - ref[0], pt[1] - ref[1])
            self.assertLessEqual(dev, err + gap + 1e-9)

    def test_error_bound_shrinks_with_tol(self):
        segs = parse("M 0 0 C 3 7 8 -4 12 5")
        total, _ = path_length(segs, tol=1e-6)
        _, e1 = point_at_length(segs, total / 2, tol=1e-3)
        _, e2 = point_at_length(segs, total / 2, tol=1e-8)
        self.assertLess(e2, e1)


if __name__ == "__main__":
    unittest.main()
