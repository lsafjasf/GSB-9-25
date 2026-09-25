"""生成修复前后对照输出（对照同一份混合数据）。"""

import table_align
import table_align_buggy

ROWS = [
    ["姓名", "cafe\u0301", "👍🏽", "a\u200bb", "x\ty"],
    ["Alice", "cafe", "👨\u200d👩\u200d👧", "abc", "ok"],
    ["李雷", "naïve", "❤️", "ab", "z"],
]

RULER = "0         1         2         3\n0123456789012345678901234567890"


def main():
    print("数据覆盖五类问题：宽字符(姓名/李雷)、组合序列(cafe+U+0301)、")
    print("表情符号(👍🏽/👨‍👩‍👧/❤️)、零宽字符(a+ZWSP+b)、制表符(x\\ty)")
    print()
    print("=== 修复前（table_align_buggy，len() 当宽度）===")
    print(RULER)
    print(table_align_buggy.render_table(ROWS))
    print()
    print("=== 修复后（table_align，统一 display_width）===")
    print(RULER)
    print(table_align.render_table(ROWS))
    print()
    print("=== 截断对照：truncate(👨‍👩‍👧‍👦ab, 3) ===")
    family = "👨\u200d👩\u200d👧\u200d👦"
    print("修复前: %r  # 切碎了 ZWJ 序列" % table_align_buggy.truncate(family + "ab", 3))
    print("修复后: %r  # 整簇保留，宽度=3" % table_align.truncate(family + "ab", 3))


if __name__ == "__main__":
    main()
