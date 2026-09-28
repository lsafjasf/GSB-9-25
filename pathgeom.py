"""pathgeom - SVG 风格矢量路径数据解析与几何计算库（仅标准库）。

功能：
  - 解析移动(M/m)、直线(L/l/H/h/V/v)、二次曲线(Q/q/T/t)、三次曲线(C/c/S/s)、
    闭合(Z/z) 指令，支持相对坐标与连续隐式指令（坐标累加严格按 SVG 规范）。
  - 统一转换为段序列：Move / Line / Quad / Cubic（Z 转为回到子路径起点的 Line）。
  - 包围盒：对二次/三次曲线按导数求精确极值，严格不低估真实曲线范围。
  - 长度：按可配置细分数 n 的折线逼近，误差上界为 M/(2n)，
    其中 M 为该段二阶导数模长在 [0,1] 上的最大值（可精确计算，见 README）。

点用复数表示：x + y*1j。
"""

import math
import re

__all__ = [
    "Move", "Line", "Quad", "Cubic", "Path",
    "parse_path", "path_bbox", "path_length", "path_length_error_bound",
]

# ---------------------------------------------------------------------------
# 段类型
# ---------------------------------------------------------------------------

class Move(object):
    """抬笔移动，无几何贡献。"""
    kind = "M"

    def __init__(self, p):
        self.p0 = p
        self.p1 = p

    def __repr__(self):
        return "Move(%r)" % (self.p0,)


class Line(object):
    kind = "L"

    def __init__(self, p0, p1):
        self.p0 = p0
        self.p1 = p1

    def point_at(self, t):
        return self.p0 + (self.p1 - self.p0) * t

    def bbox(self):
        return (min(self.p0.real, self.p1.real),
                min(self.p0.imag, self.p1.imag),
                max(self.p0.real, self.p1.real),
                max(self.p0.imag, self.p1.imag))

    def control_bbox(self):
        return self.bbox()

    def second_derivative_bound(self):
        return 0.0

    def length(self, n=1):
        return abs(self.p1 - self.p0)

    def length_error_bound(self, n=1):
        return 0.0

    def __repr__(self):
        return "Line(%r, %r)" % (self.p0, self.p1)


class Quad(object):
    """二次贝塞尔：p0 起点, p1 控制点, p2 终点。"""
    kind = "Q"

    def __init__(self, p0, p1, p2):
        self.p0 = p0
        self.p1 = p1
        self.p2 = p2

    def point_at(self, t):
        mt = 1.0 - t
        return mt * mt * self.p0 + 2.0 * mt * t * self.p1 + t * t * self.p2

    def _extrema_ts(self):
        # B'(t) 各分量为一次函数，逐个坐标轴求零点。
        ts = []
        for a, b, c in ((self.p0.real, self.p1.real, self.p2.real),
                        (self.p0.imag, self.p1.imag, self.p2.imag)):
            denom = a - 2.0 * b + c
            if denom != 0.0:
                t = (a - b) / denom
                if 0.0 < t < 1.0:
                    ts.append(t)
        return ts

    def bbox(self):
        xs = [self.p0.real, self.p2.real]
        ys = [self.p0.imag, self.p2.imag]
        for t in self._extrema_ts():
            p = self.point_at(t)
            xs.append(p.real)
            ys.append(p.imag)
        return (min(xs), min(ys), max(xs), max(ys))

    def control_bbox(self):
        xs = [self.p0.real, self.p1.real, self.p2.real]
        ys = [self.p0.imag, self.p1.imag, self.p2.imag]
        return (min(xs), min(ys), max(xs), max(ys))

    def second_derivative_bound(self):
        # B''(t) = 2*(p2 - 2*p1 + p0)，常数。
        return 2.0 * abs(self.p2 - 2.0 * self.p1 + self.p0)

    def length(self, n=64):
        return _polyline_length(self, n)

    def length_error(self, n):
        return self.second_derivative_bound() / (2.0 * n)

    length_error_bound = length_error

    def __repr__(self):
        return "Quad(%r, %r, %r)" % (self.p0, self.p1, self.p2)


class Cubic(object):
    """三次贝塞尔：p0 起点, p1/p2 控制点, p3 终点。"""
    kind = "C"

    def __init__(self, p0, p1, p2, p3):
        self.p0 = p0
        self.p1 = p1
        self.p2 = p2
        self.p3 = p3

    def point_at(self, t):
        mt = 1.0 - t
        return (mt * mt * mt * self.p0 + 3.0 * mt * mt * t * self.p1
                + 3.0 * mt * t * t * self.p2 + t * t * t * self.p3)

    def _extrema_ts(self):
        # B'(t) 各分量为二次函数：A t^2 + B t + C = 0，逐轴求解。
        ts = []
        for a, b, c, d in ((self.p0.real, self.p1.real, self.p2.real, self.p3.real),
                           (self.p0.imag, self.p1.imag, self.p2.imag, self.p3.imag)):
            A = -a + 3.0 * b - 3.0 * c + d
            B = 2.0 * (a - 2.0 * b + c)
            C = b - a
            if abs(A) < 1e-18:
                if B != 0.0:
                    t = -C / B
                    if 0.0 < t < 1.0:
                        ts.append(t)
            else:
                disc = B * B - 4.0 * A * C
                if disc >= 0.0:
                    sq = math.sqrt(disc)
                    for t in ((-B + sq) / (2.0 * A), (-B - sq) / (2.0 * A)):
                        if 0.0 < t < 1.0:
                            ts.append(t)
        return ts

    def bbox(self):
        xs = [self.p0.real, self.p3.real]
        ys = [self.p0.imag, self.p3.imag]
        for t in self._extrema_ts():
            p = self.point_at(t)
            xs.append(p.real)
            ys.append(p.imag)
        return (min(xs), min(ys), max(xs), max(ys))

    def control_bbox(self):
        xs = [self.p0.real, self.p1.real, self.p2.real, self.p3.real]
        ys = [self.p0.imag, self.p1.imag, self.p2.imag, self.p3.imag]
        return (min(xs), min(ys), max(xs), max(ys))

    def second_derivative_bound(self):
        # B''(t) = 6(1-t)(p2 - 2p1 + p0) + 6t(p3 - 2p2 + p1)，关于 t 线性，
        # 模长最大值必在端点取得。
        return 6.0 * max(abs(self.p2 - 2.0 * self.p1 + self.p0),
                         abs(self.p3 - 2.0 * self.p2 + self.p1))

    def length(self, n=64):
        return _polyline_length(self, n)

    def length_error(self, n):
        return self.second_derivative_bound() / (2.0 * n)

    length_error_bound = length_error

    def __repr__(self):
        return "Cubic(%r, %r, %r, %r)" % (self.p0, self.p1, self.p2, self.p3)


def _polyline_length(seg, n):
    if n < 1:
        raise ValueError("subdivision count n must be >= 1")
    total = 0.0
    prev = seg.point_at(0.0)
    for k in range(1, n + 1):
        cur = seg.point_at(k / n)
        total += abs(cur - prev)
        prev = cur
    return total


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"[AaCcHhLlMmQqSsTtVvZz]"
    r"|[-+]?(?:[0-9]*\.[0-9]+|[0-9]+\.?)(?:[eE][-+]?[0-9]+)?")

_ARG_COUNT = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2}


def _tokenize(d):
    tokens = []
    pos = 0
    for m in _TOKEN_RE.finditer(d):
        gap = d[pos:m.start()]
        if gap.strip(" \t\r\n,"):
            raise ValueError("invalid character in path data: %r" % gap)
        tokens.append(m.group(0))
        pos = m.end()
    if d[pos:].strip(" \t\r\n,"):
        raise ValueError("invalid trailing data: %r" % d[pos:])
    return tokens


def parse_path(d):
    """解析路径数据字符串，返回段列表（Move/Line/Quad/Cubic）。"""
    tokens = _tokenize(d)
    segments = []
    i = 0
    n = len(tokens)
    cmd = None
    cur = 0j          # 当前点
    start = 0j        # 当前子路径起点
    prev_cubic_ctrl = None
    prev_quad_ctrl = None
    last_kind = None

    def read_point(x, y, rel):
        p = complex(x, y)
        return p + cur if rel else p

    while i < n:
        tok = tokens[i]
        if tok.isalpha():
            cmd = tok
            i += 1
            if cmd in "Zz":
                # 闭合：画一条回到子路径起点的直线（可能是零长度段）。
                segments.append(Line(cur, start))
                cur = start
                cmd = None
                last_kind = "Z"
                prev_cubic_ctrl = prev_quad_ctrl = None
                continue
        if cmd is None:
            raise ValueError("path data must begin with a command")
        c = cmd.upper()
        if c not in _ARG_COUNT:
            raise ValueError("unsupported command %r (arcs A/a not supported)" % cmd)
        rel = cmd.islower()
        arity = _ARG_COUNT[c]
        # 命令级最小参数校验必须先于取数：命令后一个参数都没有时（串尾或紧跟
        # 下一条命令字母）内层循环一次都不会转，循环内的检查无法触发。
        if i >= n or tokens[i].isalpha():
            raise ValueError(
                "command %r expects %d numbers, got none" % (cmd, arity))
        first_moveto = True
        while i < n and not tokens[i].isalpha():
            if i + arity > n or any(tokens[i + k].isalpha() for k in range(arity)):
                raise ValueError("command %r expects %d numbers" % (cmd, arity))
            args = [float(tokens[i + k]) for k in range(arity)]
            i += arity

            if c == "M":
                p = read_point(args[0], args[1], rel)
                if first_moveto:
                    segments.append(Move(p))
                    start = p
                    last_kind = "M"
                    first_moveto = False
                else:
                    # 连续的后续坐标对按隐式 L/l 处理（相对坐标逐次累加）。
                    segments.append(Line(cur, p))
                    last_kind = "L"
                cur = p
                prev_cubic_ctrl = prev_quad_ctrl = None
            elif c == "L":
                p = read_point(args[0], args[1], rel)
                segments.append(Line(cur, p))
                cur = p
                last_kind = "L"
                prev_cubic_ctrl = prev_quad_ctrl = None
            elif c == "H":
                x = args[0] + (cur.real if rel else 0.0)
                p = complex(x, cur.imag)
                segments.append(Line(cur, p))
                cur = p
                last_kind = "L"
                prev_cubic_ctrl = prev_quad_ctrl = None
            elif c == "V":
                y = args[0] + (cur.imag if rel else 0.0)
                p = complex(cur.real, y)
                segments.append(Line(cur, p))
                cur = p
                last_kind = "L"
                prev_cubic_ctrl = prev_quad_ctrl = None
            elif c == "C":
                c1 = read_point(args[0], args[1], rel)
                c2 = read_point(args[2], args[3], rel)
                p = read_point(args[4], args[5], rel)
                segments.append(Cubic(cur, c1, c2, p))
                prev_cubic_ctrl = c2
                prev_quad_ctrl = None
                cur = p
                last_kind = "C"
            elif c == "S":
                c1 = (2.0 * cur - prev_cubic_ctrl) if last_kind == "C" else cur
                c2 = read_point(args[0], args[1], rel)
                p = read_point(args[2], args[3], rel)
                segments.append(Cubic(cur, c1, c2, p))
                prev_cubic_ctrl = c2
                prev_quad_ctrl = None
                cur = p
                last_kind = "C"
            elif c == "Q":
                q = read_point(args[0], args[1], rel)
                p = read_point(args[2], args[3], rel)
                segments.append(Quad(cur, q, p))
                prev_quad_ctrl = q
                prev_cubic_ctrl = None
                cur = p
                last_kind = "Q"
            elif c == "T":
                q = (2.0 * cur - prev_quad_ctrl) if last_kind == "Q" else cur
                p = read_point(args[0], args[1], rel)
                segments.append(Quad(cur, q, p))
                prev_quad_ctrl = q
                prev_cubic_ctrl = None
                cur = p
                last_kind = "Q"
    return segments


# ---------------------------------------------------------------------------
# 路径级几何
# ---------------------------------------------------------------------------

def _drawing_segments(segments):
    return [s for s in segments if not isinstance(s, Move)]


def path_bbox(segments):
    """所有绘制段的精确包围盒 (minx, miny, maxx, maxy)；无绘制段返回 None。"""
    boxes = [s.bbox() for s in _drawing_segments(segments)]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def path_control_bbox(segments):
    """控制点包围盒（保守上界，保证不低估）。"""
    boxes = [s.control_bbox() for s in _drawing_segments(segments)]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def path_length(segments, n=64):
    """折线逼近总长，每段细分为 n 份。"""
    return sum(s.length(n) for s in _drawing_segments(segments))


def path_length_error_bound(segments, n=64):
    """长度误差上界：sum(M_seg / (2n))，推导见 README。"""
    return sum(s.length_error_bound(n) for s in _drawing_segments(segments))


class Path(object):
    """便捷封装：Path(d).bbox() / .length(n) / .length_error_bound(n)。"""

    def __init__(self, d):
        self.d = d
        self.segments = parse_path(d)

    def bbox(self):
        return path_bbox(self.segments)

    def control_bbox(self):
        return path_control_bbox(self.segments)

    def length(self, n=64):
        return path_length(self.segments, n)

    def length_error_bound(self, n=64):
        return path_length_error_bound(self.segments, n)

    def __repr__(self):
        return "Path(%d segments)" % len(self.segments)
