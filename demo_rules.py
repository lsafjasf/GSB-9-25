"""断行规则插件演示：同一段混排文本在不同规则下的断行对照表与真实输出。

运行：python3 demo_rules.py [行宽]
"""

import sys

from displaywrap import display_width, list_rules, wrap

TEXT = "他说：“排版（typesetting）要避头尾，否则punctuation、brackets会错位。”"
RULE_SETS = ["default", "cjk", "western", ("cjk", "western")]


def rule_name(rules):
    return rules if isinstance(rules, str) else "+".join(rules)


def pad_display(s, width):
    """按显示宽度右补空格（对照表列对齐用）。"""
    return s + " " * max(0, width - display_width(s))


def main():
    width = int(sys.argv[1]) if len(sys.argv) > 1 else 9
    print(f"文本（显示宽 {display_width(TEXT)} 列）：{TEXT}")
    print(f"行宽：{width} 列；可用规则：{', '.join(list_rules())}")
    print(f"对照规则集：{', '.join(rule_name(r) for r in RULE_SETS)}")
    print()

    results = {rule_name(r): wrap(TEXT, width, rules=r) for r in RULE_SETS}

    # ---- 断行对照表（每列一种规则，行内为各规则的第 N 行）----
    names = list(results)
    col_w = width + 4
    header = "  ".join(pad_display(f"[{n}]", col_w) for n in names)
    print("断行对照表".center(len(header)))
    print(header)
    print("  ".join("-" * col_w for _ in names))
    nrows = max(len(v) for v in results.values())
    for i in range(nrows):
        row = []
        for n in names:
            cell = results[n][i] if i < len(results[n]) else ""
            row.append(pad_display(cell, col_w))
        print("  ".join(row))
    print()

    # ---- 真实输出（逐行原样打印，| 为行宽参考线）----
    print("真实输出（两侧 │ 为行宽参考线；~ 标记超宽悬挂行）：")
    ruler = "│" + " " * width + "│"
    for n in names:
        print(f"== rules={n} ==")
        print(f"  {ruler}")
        for line in results[n]:
            w = display_width(line)
            mark = " ~" if w > width else ""
            print(f"  │{pad_display(line, width)}│{mark}  ({w}列)")
        print()

    print(f"校验：display_width(原文) = {display_width(TEXT)}，与所选规则无关。")


if __name__ == "__main__":
    main()
