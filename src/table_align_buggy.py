"""缺陷版本（现网逻辑快照，仅用于复现与对照，请勿再修改）。

已知缺陷：
1. 用 len() 当显示宽度，宽字符（中文等）行整体右移；
2. 组合字符 / 表情符号按码点计数，宽度算错；
3. 制表符不展开，把后续列推歪；
4. 零宽字符被计入宽度，后续列错位；
5. 截断按码点下标切片，会把不可拆散序列切成半个。
宽度逻辑在 column_widths / render / truncate / wrap 中各自重复实现。
"""

PAD = 2  # 列间距


def column_widths(rows):
    widths = []
    for row in rows:
        for i, cell in enumerate(row):
            w = len(cell)  # 缺陷：字符数当显示宽度
            if i == len(widths):
                widths.append(w)
            else:
                widths[i] = max(widths[i], w)
    return widths


def render(rows):
    widths = column_widths(rows)
    lines = []
    for row in rows:
        parts = []
        for i, cell in enumerate(row):
            if i < len(row) - 1:
                parts.append(cell + " " * (widths[i] - len(cell) + PAD))  # 缺陷：重复实现宽度
            else:
                parts.append(cell)
        lines.append("".join(parts))
    return lines


def truncate(text, max_width):
    if len(text) <= max_width:  # 缺陷：重复实现宽度
        return text
    return text[:max_width]  # 缺陷：可能切开组合序列 / 表情 ZWJ 序列


def wrap(text, max_width):
    lines = []
    current = ""
    for ch in text:
        if len(current) >= max_width:  # 缺陷：第三处重复实现宽度
            lines.append(current)
            current = ""
        current += ch
    if current:
        lines.append(current)
    return lines
