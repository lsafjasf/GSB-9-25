"""SVG 路径数据解析器。

支持指令: M/m, L/l, H/h, V/v, Q/q, T/t, C/c, S/s, Z/z。
所有指令统一转换为绝对坐标的段序列 (Segment 列表)。
不支持 A/a (圆弧) 指令, 遇到时抛出 ValueError。
"""

import re
from dataclasses import dataclass
from typing import List, Tuple

Point = Tuple[float, float]

_TOKEN_RE = re.compile(
    r"(?P<cmd>[MmLlHhVvCcSsQqTtZzAa])"
    r"|(?P<num>[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)"
)

_SEPARATORS = set(" \t\n\r,")

# 每个参数组需要的参数个数
_PARAMS = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2}


@dataclass(frozen=True)
class Segment:
    """一段路径。kind 为 'M'/'L'/'Q'/'C'/'Z'，points 为绝对坐标点元组。

    - M: (p,)
    - L: (p0, p1)
    - Q: (p0, p1, p2)      二次贝塞尔
    - C: (p0, p1, p2, p3)  三次贝塞尔
    - Z: (p0, p1)          闭合段, 等价于回到子路径起点的直线
    """

    kind: str
    points: Tuple[Point, ...]


def _tokenize(d: str) -> List:
    tokens: List = []
    pos = 0
    for m in _TOKEN_RE.finditer(d):
        gap = d[pos : m.start()]
        if any(ch not in _SEPARATORS for ch in gap):
            raise ValueError(f"路径数据含非法字符: {gap!r}")
        pos = m.end()
        if m.group("cmd"):
            tokens.append(m.group("cmd"))
        else:
            tokens.append(float(m.group("num")))
    tail = d[pos:]
    if any(ch not in _SEPARATORS for ch in tail):
        raise ValueError(f"路径数据含非法字符: {tail!r}")
    return tokens


def parse(d: str) -> List[Segment]:
    """解析 SVG 路径数据字符串, 返回绝对坐标的 Segment 列表。"""
    tokens = _tokenize(d)
    segments: List[Segment] = []
    cur: Point = (0.0, 0.0)          # 当前点
    start: Point = (0.0, 0.0)        # 当前子路径起点
    prev_ctrl = None                 # 上一条曲线段的最后一个控制点 (用于 S/T 平滑)
    prev_curve = ""                  # 上一条段所属曲线族: 'C' / 'Q' / ''
    i, n = 0, len(tokens)
    cmd = None

    while i < n:
        tok = tokens[i]
        if isinstance(tok, str):
            cmd = tok
            i += 1
            if cmd in "Zz":
                segments.append(Segment("Z", (cur, start)))
                cur = start
                prev_ctrl, prev_curve = None, ""
                cmd = None
                continue
            if cmd in "Aa":
                raise ValueError("不支持圆弧指令 (A/a)")
        elif cmd is None:
            raise ValueError("路径数据必须以指令开头")

        upper = cmd.upper()
        rel = cmd.islower()
        consumed = 0

        while True:
            k = _PARAMS[upper]
            if i + k > n or any(isinstance(t, str) for t in tokens[i : i + k]):
                break
            args = tokens[i : i + k]
            i += k
            consumed += 1

            if upper == "M":
                p = (args[0] + (cur[0] if rel else 0.0),
                     args[1] + (cur[1] if rel else 0.0))
                segments.append(Segment("M", (p,)))
                cur = start = p
                prev_ctrl, prev_curve = None, ""
                # moveto 之后的隐式参数组按 lineto 处理 (相对性不变)
                upper = "L"
            elif upper == "L":
                p = (args[0] + (cur[0] if rel else 0.0),
                     args[1] + (cur[1] if rel else 0.0))
                segments.append(Segment("L", (cur, p)))
                cur = p
                prev_ctrl, prev_curve = None, ""
            elif upper == "H":
                x = args[0] + (cur[0] if rel else 0.0)
                p = (x, cur[1])
                segments.append(Segment("L", (cur, p)))
                cur = p
                prev_ctrl, prev_curve = None, ""
            elif upper == "V":
                y = args[0] + (cur[1] if rel else 0.0)
                p = (cur[0], y)
                segments.append(Segment("L", (cur, p)))
                cur = p
                prev_ctrl, prev_curve = None, ""
            elif upper == "C":
                c1 = (args[0] + (cur[0] if rel else 0.0),
                      args[1] + (cur[1] if rel else 0.0))
                c2 = (args[2] + (cur[0] if rel else 0.0),
                      args[3] + (cur[1] if rel else 0.0))
                p = (args[4] + (cur[0] if rel else 0.0),
                     args[5] + (cur[1] if rel else 0.0))
                segments.append(Segment("C", (cur, c1, c2, p)))
                cur = p
                prev_ctrl, prev_curve = c2, "C"
            elif upper == "S":
                if prev_curve == "C" and prev_ctrl is not None:
                    c1 = (2 * cur[0] - prev_ctrl[0], 2 * cur[1] - prev_ctrl[1])
                else:
                    c1 = cur
                c2 = (args[0] + (cur[0] if rel else 0.0),
                      args[1] + (cur[1] if rel else 0.0))
                p = (args[2] + (cur[0] if rel else 0.0),
                     args[3] + (cur[1] if rel else 0.0))
                segments.append(Segment("C", (cur, c1, c2, p)))
                cur = p
                prev_ctrl, prev_curve = c2, "C"
            elif upper == "Q":
                c = (args[0] + (cur[0] if rel else 0.0),
                     args[1] + (cur[1] if rel else 0.0))
                p = (args[2] + (cur[0] if rel else 0.0),
                     args[3] + (cur[1] if rel else 0.0))
                segments.append(Segment("Q", (cur, c, p)))
                cur = p
                prev_ctrl, prev_curve = c, "Q"
            elif upper == "T":
                if prev_curve == "Q" and prev_ctrl is not None:
                    c = (2 * cur[0] - prev_ctrl[0], 2 * cur[1] - prev_ctrl[1])
                else:
                    c = cur
                p = (args[0] + (cur[0] if rel else 0.0),
                     args[1] + (cur[1] if rel else 0.0))
                segments.append(Segment("Q", (cur, c, p)))
                cur = p
                prev_ctrl, prev_curve = c, "Q"

        if consumed == 0 and cmd is not None:
            raise ValueError(f"指令 {cmd!r} 缺少参数")

    return segments
