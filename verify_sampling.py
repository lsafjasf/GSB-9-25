"""采样检验脚本：验证包围盒严格不低估真实曲线范围。

方法：对大量确定性随机路径（含 L/Q/C/Z、绝对/相对混合），
  1. 计算每段的精确包围盒（导数极值法）；
  2. 在每段参数域上密集采样（默认每段 2000 点）；
  3. 断言所有采样点都落在包围盒内（允许 1e-9 浮点余量）；
  4. 同时验证控制点包围盒 ⊇ 精确包围盒（保守性）。

运行：python3 verify_sampling.py [路径数] [每段采样点数]
"""

import random
import sys

from pathgeom import Move, parse_path, path_bbox

EPS = 1e-9


def random_path(rng, n_cmds):
    """生成随机路径字符串，混合绝对/相对指令与闭合。"""
    parts = ["M %.6f %.6f" % (rng.uniform(-500, 500), rng.uniform(-500, 500))]
    for _ in range(n_cmds):
        cmd = rng.choice("LlQqCcSsTtHhVv")
        if cmd in "Ll":
            parts.append("%s %.6f %.6f" % (cmd, rng.uniform(-100, 100),
                                           rng.uniform(-100, 100)))
        elif cmd in "HhVv":
            parts.append("%s %.6f" % (cmd, rng.uniform(-100, 100)))
        elif cmd in "QqTt":
            parts.append("%s %.6f %.6f %.6f %.6f" %
                         (cmd, *([rng.uniform(-150, 150) for _ in range(4)])))
        elif cmd in "Ss":
            parts.append("%s %.6f %.6f %.6f %.6f" %
                         (cmd, *([rng.uniform(-150, 150) for _ in range(4)])))
        else:  # Cc
            parts.append("%s %.6f %.6f %.6f %.6f %.6f %.6f" %
                         (cmd, *([rng.uniform(-150, 150) for _ in range(6)])))
        if rng.random() < 0.05:
            parts.append("Z")
            parts.append("m %.6f %.6f" % (rng.uniform(-500, 500),
                                          rng.uniform(-500, 500)))
    return " ".join(parts)


def main():
    n_paths = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    samples = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    rng = random.Random(20260927)

    total_segs = 0
    total_points = 0
    worst_margin = float("inf")  # 采样点到盒边的最小距离（越小越贴近）
    for _ in range(n_paths):
        d = random_path(rng, rng.randint(1, 40))
        segs = parse_path(d)
        for s in segs:
            if isinstance(s, Move):
                continue
            total_segs += 1
            x0, y0, x1, y1 = s.bbox()
            cx0, cy0, cx1, cy1 = s.control_bbox()
            assert cx0 <= x0 + EPS and cy0 <= y0 + EPS
            assert cx1 >= x1 - EPS and cy1 >= y1 - EPS
            for k in range(samples + 1):
                p = s.point_at(k / samples)
                total_points += 1
                assert x0 - EPS <= p.real <= x1 + EPS, (s, p, (x0, y0, x1, y1))
                assert y0 - EPS <= p.imag <= y1 + EPS, (s, p, (x0, y0, x1, y1))
                w = x1 - x0
                h = y1 - y0
                if w > 0:
                    worst_margin = min(worst_margin,
                                       (p.real - x0) / w, (x1 - p.real) / w)
                if h > 0:
                    worst_margin = min(worst_margin,
                                       (p.imag - y0) / h, (y1 - p.imag) / h)

    print("采样检验通过：%d 条路径，%d 个绘制段，%d 个采样点"
          % (n_paths, total_segs, total_points))
    print("所有采样点均在精确包围盒内（容差 1e-9），未出现盒外点。")
    print("控制点包围盒均包含精确包围盒（保守性成立）。")
    print("采样点到盒边最小相对距离：%.3e（>0 且可接近 0，"
          "说明包围盒紧贴曲线、无系统性高估）" % max(worst_margin, 0.0))


if __name__ == "__main__":
    main()
