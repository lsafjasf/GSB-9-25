"""修复前快照：按字符逐个累加宽度（仅用于对照，请勿再修改）。

已知问题：家庭表情（ZWJ 序列）被算成 6 列、带肤色/变体选择符的表情被算成
4 列、区域指示符对被拆成两个字符各算 1 列，导致含这些字符的行整体错位。
修复版见 table_align.py（按字素簇计宽）。

原实现说明：

宽度规则（唯一定义在 display_width / _char_width，全流程复用）：
- 制表符：一律按 8 列位展开为空格（expand_tabs），对齐/截断/折行一致；
- 零宽字符（Mn/Me/Cf、组合记号、变体选择符、肤色修饰符等）：宽度 0；
- 东亚宽字符（east_asian_width 为 W/F，含绝大多数表情符号）：宽度 2；
- 其余可打印字符：宽度 1。

截断与折行按“集群”（base + 组合记号 / ZWJ 序列 / 旗帜对）推进，
保证不可拆散序列永远不会被切成半个。
"""

import unicodedata

TAB_STOP = 8  # 制表符策略：按列位展开
PAD = 2       # 列间距

_ZWJ = 0x200D


def _is_extend(ch):
    """组合/附着类字符：并入前一个集群且不占宽度。"""
    if unicodedata.combining(ch):
        return True
    if unicodedata.category(ch) in ("Mn", "Me"):
        return True
    cp = ord(ch)
    return (
        0xFE00 <= cp <= 0xFE0F      # 变体选择符
        or 0x1F3FB <= cp <= 0x1F3FF  # emoji 肤色修饰符
        or cp == 0x20E3              # keycap
    )


def _is_regional(ch):
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def clusters(text):
    """把文本切成不可拆散的集群（grapheme 的简化实现，仅标准库）。"""
    result = []
    current = ""
    prev_zwj = False
    regional_run = 0
    for ch in text:
        if not current:
            current = ch
            regional_run = 1 if _is_regional(ch) else 0
            continue
        if prev_zwj or ord(ch) == _ZWJ or _is_extend(ch):
            current += ch
            prev_zwj = ord(ch) == _ZWJ
            continue
        if _is_regional(ch) and regional_run % 2 == 1:
            current += ch  # 旗帜：两个 regional indicator 一对
            regional_run += 1
            continue
        result.append(current)
        current = ch
        regional_run = 1 if _is_regional(ch) else 0
        prev_zwj = False
    if current:
        result.append(current)
    return result


def _char_width(ch):
    if ch == "\t":
        raise ValueError("制表符必须先经 expand_tabs 展开")
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Cf") or unicodedata.combining(ch):
        return 0
    if cat in ("Cc", "Cs"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def expand_tabs(text, tab_stop=TAB_STOP):
    """把制表符按列位展开为空格（基于显示列，而非字符数）。"""
    out = []
    col = 0
    for ch in text:
        if ch == "\t":
            n = tab_stop - (col % tab_stop)
            out.append(" " * n)
            col += n
        else:
            out.append(ch)
            col += _char_width(ch)
    return "".join(out)


def display_width(text):
    """唯一的显示宽度来源：先展开制表符，再按集群内字符宽度求和。"""
    return sum(_char_width(ch) for ch in expand_tabs(text))


def truncate(text, max_width):
    """按显示宽度截断，绝不切开集群。"""
    text = expand_tabs(text)
    out = []
    used = 0
    for cluster in clusters(text):
        w = display_width(cluster)
        if used + w > max_width:
            break
        out.append(cluster)
        used += w
    return "".join(out)


def wrap(text, max_width):
    """按显示宽度折行，集群不可拆。"""
    text = expand_tabs(text)
    lines = []
    current = []
    used = 0
    for cluster in clusters(text):
        w = display_width(cluster)
        if used + w > max_width and current:
            lines.append("".join(current))
            current = []
            used = 0
        current.append(cluster)
        used += w
    if current:
        lines.append("".join(current))
    return lines


def column_widths(rows):
    widths = []
    for row in rows:
        for i, cell in enumerate(row):
            w = display_width(cell)
            if i == len(widths):
                widths.append(w)
            else:
                widths[i] = max(widths[i], w)
    return widths


def render(rows):
    """按列对齐渲染，每列左侧对齐、列间 PAD 个空格。"""
    widths = column_widths(rows)
    lines = []
    for row in rows:
        parts = []
        for i, cell in enumerate(row):
            cell = expand_tabs(cell)
            if i < len(row) - 1:
                parts.append(cell + " " * (widths[i] - display_width(cell) + PAD))
            else:
                parts.append(cell)
        lines.append("".join(parts))
    return lines
