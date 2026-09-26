"""复现脚本：针对 naive_parser（现网版本）演示五类缺陷。

运行：python3 reproduce.py
只要缺陷仍然存在，脚本就以退出码 1 结束；全部修复后退出码 0。
"""

import naive_parser

failures = []


def check(title, actual, expected):
    ok = actual == expected
    status = "OK  " if ok else "BUG "
    print(f"[{status}] {title}")
    print(f"       期望: {expected!r}")
    print(f"       实际: {actual!r}")
    if not ok:
        failures.append(title)


# 缺陷 1：引号内的空格被截断
check(
    "1. 双引号值中的空格应保留",
    naive_parser.parse_line('GREETING="hello world"'),
    ("GREETING", "hello world"),
)

# 缺陷 2：单引号内不应做转义，反斜杠必须原样保留
check(
    "2. 单引号内反斜杠应原样保留",
    naive_parser.parse_line(r"PATH='C:\new\tmp'"),
    ("PATH", r"C:\new\tmp"),
)

# 缺陷 3：空值与值缺失必须能区分
empty = naive_parser.parse_line("A=")
missing = naive_parser.parse_line("B")
check(
    "3. 空值(A='')与值缺失(B)应可区分",
    (empty, missing),
    (("A", ""), "应该报错或返回 has_value=False"),
)

# 缺陷 4：引号内的 # 不应被当注释
check(
    "4. 引号内的 # 应保留",
    naive_parser.parse_line("TAG='v1 # not a comment'"),
    ("TAG", "v1 # not a comment"),
)

# 缺陷 5：非法行应报出行号与列位置
try:
    naive_parser.parse_line("=oops", line_no=7)
    check("5. 键为空应报错", "未报错", "带行号/列位置的错误")
except ValueError as exc:
    has_pos = hasattr(exc, "line") and hasattr(exc, "column")
    check(
        "5. 错误应携带行号与列位置",
        (str(exc), getattr(exc, "line", None), getattr(exc, "column", None)),
        ("错误信息 + line=7 + column 指向 '='",) if has_pos else "带行号/列位置的错误",
    )

print()
if failures:
    print(f"复现成功：共 {len(failures)} 类缺陷仍然存在。")
    raise SystemExit(1)
print("未发现缺陷。")
