"""文本表格对齐工具 —— 修复后版本。

宽度规则（唯一来源：display_width / cluster_width，全流程复用）：
- 以“字素簇（grapheme cluster）”为最小单位，绝不从簇中间截断。
- 宽字符（East Asian Width = W/F）：宽度 2。
- 组合记号（Mn/Me）、零宽字符（Cf，如 ZWSP/ZWJ/ZWNJ）、
  变体选择符、emoji 肤色修饰符：宽度 0。
- ZWJ 序列、emoji 表现序列（含 U+FE0F）、区域指示符对（国旗）：宽度 2。
- 其余可打印字符：宽度 1。
- 制表符策略：一律按 8 列一个制表位展开为空格（expand_tabs），
  对齐、截断、折行、宽度计算全部基于展开后的文本，输出不含 '\\t'。
"""

import unicodedata

PADDING = 2
TAB_SIZE = 8

ZWJ = "\u200d"
VS15 = "\ufe0e"  # 文本表现
VS16 = "\ufe0f"  # emoji 表现


def _is_variation_selector(ch):
    cp = ord(ch)
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF


def _is_regional_indicator(ch):
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_emoji_modifier(ch):
    return 0x1F3FB <= ord(ch) <= 0x1F3FF


def _is_extend(ch):
    """应附着在前一个基础字符上的字符。"""
    return (
        unicodedata.combining(ch) != 0
        or unicodedata.category(ch) in ("Mn", "Me")
        or _is_variation_selector(ch)
        or _is_emoji_modifier(ch)
    )


def grapheme_clusters(text):
    """把字符串切分为字素簇（简化版 UAX #29，覆盖常见情形）。"""
    clusters = []
    for ch in text:
        if not clusters:
            clusters.append(ch)
            continue
        prev = clusters[-1]
        if (
            _is_extend(ch)
            or ch == ZWJ
            or prev.endswith(ZWJ)  # ZWJ 后面的字符并入同一簇
            or (
                _is_regional_indicator(ch)
                and len(prev) == 1
                and _is_regional_indicator(prev)
            )
        ):
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def _char_width(ch):
    if ch == "\t":
        raise ValueError("制表符必须先经 expand_tabs 展开")
    if (
        unicodedata.combining(ch) != 0
        or unicodedata.category(ch) in ("Mn", "Me", "Cf", "Cc")
        or _is_variation_selector(ch)
        or _is_emoji_modifier(ch)
    ):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def cluster_width(cluster):
    """单个字素簇的显示宽度。"""
    if VS16 in cluster:
        return 2
    if VS15 in cluster:
        return 1
    if ZWJ in cluster and any(_char_width(c) == 2 for c in cluster):
        return 2
    if _is_regional_indicator(cluster[0]):
        return 2
    return sum(_char_width(c) for c in cluster)


def expand_tabs(text, tab_size=TAB_SIZE):
    """把制表符按列位展开为空格（每行独立计列）。"""
    return "\n".join(_expand_tabs_line(line, tab_size) for line in text.split("\n"))


def _expand_tabs_line(line, tab_size):
    out = []
    col = 0
    for cluster in grapheme_clusters(line):
        if cluster == "\t":
            n = tab_size - (col % tab_size)
            out.append(" " * n)
            col += n
        else:
            out.append(cluster)
            col += cluster_width(cluster)
    return "".join(out)


def display_width(text):
    """唯一的宽度来源：所有制表符先展开，再按字素簇求和。"""
    return sum(cluster_width(c) for c in grapheme_clusters(expand_tabs(text)))


def truncate(text, max_width):
    """按显示宽度截断，绝不拆散字素簇。"""
    out = []
    used = 0
    for cluster in grapheme_clusters(expand_tabs(text)):
        w = cluster_width(cluster)
        if used + w > max_width:
            break
        out.append(cluster)
        used += w
    return "".join(out)


def wrap(text, max_width):
    """按显示宽度折行，绝不拆散字素簇。"""
    lines = []
    current = []
    used = 0
    for cluster in grapheme_clusters(expand_tabs(text)):
        w = cluster_width(cluster)
        if used + w > max_width and current:
            lines.append("".join(current))
            current, used = [], 0
        current.append(cluster)
        used += w
    if current or not lines:
        lines.append("".join(current))
    return lines


def column_widths(rows, max_col_width=None):
    ncols = max(len(r) for r in rows)
    widths = []
    for c in range(ncols):
        w = max((display_width(r[c]) for r in rows if c < len(r)), default=0)
        if max_col_width is not None:
            w = min(w, max_col_width)
        widths.append(w)
    return widths


def render_table(rows, max_col_width=None):
    widths = column_widths(rows, max_col_width)
    lines = []
    for row in rows:
        cells = []
        for c in range(len(widths)):
            cell = row[c] if c < len(row) else ""
            if max_col_width is not None:
                cell = truncate(cell, widths[c])
            else:
                cell = expand_tabs(cell)
            cells.append(cell + " " * max(0, widths[c] - display_width(cell)))
        lines.append((" " * PADDING).join(cells).rstrip())
    return "\n".join(lines)
