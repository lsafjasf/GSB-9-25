"""文本表格对齐工具 —— 修复前（有缺陷）版本。

已知缺陷（保留用于复现与前后对照）：
1. 用 len()（码点数量）当显示宽度，中文等宽字符列整体右移。
2. 组合字符 / 表情符号（ZWJ 序列、肤色修饰符、VS16）宽度算错。
3. 制表符不展开，终端里把后续列推歪。
4. 零宽字符（ZWSP/ZWJ/ZWNJ 等）被计为宽度 1，后续列错位。
5. 截断 / 折行按码点切片，可能把不可拆散序列切成半个。
"""

PADDING = 2


def cell_width(text):
    return len(text)


def pad_cell(text, width):
    return text + " " * max(0, width - len(text))


def truncate(text, max_width):
    return text[:max_width]


def wrap(text, max_width):
    return [text[i:i + max_width] for i in range(0, len(text), max_width)] or [""]


def column_widths(rows, max_col_width=None):
    ncols = max(len(r) for r in rows)
    widths = []
    for c in range(ncols):
        w = max((cell_width(r[c]) for r in rows if c < len(r)), default=0)
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
            cells.append(pad_cell(cell, widths[c]))
        lines.append((" " * PADDING).join(cells).rstrip())
    return "\n".join(lines)
