"""生成修复前后对照输出：python3 compare.py（写入 comparison_output.txt）。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import table_align as fixed
import table_align_buggy as buggy

DATASET = [
    ["abcd", "c1"],
    ["中文", "c2"],
    ["é", "c3"],       # 'e' + U+0301
    ["🙂", "c4"],
    ["ab​cd", "c5"],  # 含 U+200B
    ["a\tb", "c6"],
    ["👨‍👩‍👧", "c7"],        # ZWJ 家庭序列
    ["❤️", "c8"],            # 变体选择符表情
    ["🇨🇳", "c9"],            # 区域指示符对（旗帜）
]


def show(title, lines):
    out = [title]
    for l in lines:
        out.append("|" + l.replace("\t", "\\t") + "|")
    starts = [fixed.display_width(l[: l.index(cell)])
              for l, (_, cell) in zip(lines, DATASET)]
    out.append("第二列起始显示列: " + str(starts) +
               ("  <-- 错位" if len(set(starts)) > 1 else "  对齐 OK"))
    return "\n".join(out)


sections = []
sections.append(show("【修复前 table_align_buggy.render】", buggy.render(DATASET)))
sections.append("")
sections.append(show("【修复后 table_align.render】", fixed.render(DATASET)))
sections.append("")
sections.append("【截断对照】truncate('é', 1) / truncate('👨‍👩‍👧', 3)")
sections.append("修复前: %r / %r  （组合记号被丢掉、ZWJ 序列被切成半个）"
                % (buggy.truncate("é", 1), buggy.truncate("👨‍👩‍👧", 3)))
sections.append("修复后: %r / %r  （集群完整保留或整体舍弃）"
                % (fixed.truncate("é", 1), fixed.truncate("👨‍👩‍👧", 3)))



def old_rule_width(text):
    """修复前的宽度规则：逐字符经 _char_width 累加（ZWJ 序列被算成 6 列）。"""
    return sum(fixed._char_width(ch) for ch in fixed.expand_tabs(text))


WIDTH_CASES = ["👨‍👩‍👧", "❤️", "✈️", "1️⃣", "🇨🇳", "é", "中文", "ab​cd"]
sections.append("")
sections.append("【宽度对照】逐字符累加（修复前） vs 字素簇整体计宽（修复后）")
for s in WIDTH_CASES:
    sections.append("%-12r 修复前=%d  修复后=%d"
                    % (s, old_rule_width(s), fixed.display_width(s)))

report = "\n".join(sections) + "\n"
with open(os.path.join(os.path.dirname(__file__), "comparison_output.txt"), "w") as f:
    f.write(report)
print(report)
