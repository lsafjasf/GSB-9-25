"""复现 legacy_entity_tool 的五类现网缺陷。

运行：python3 reproduce_defects.py
五类缺陷全部复现时退出码为 0；任一缺陷不再复现（例如误指向修复版）则退出码为 1。
"""

import sys

import legacy_entity_tool as legacy

defects = []

# 缺陷 1：已转义内容被二次转义
once = legacy.encode("a < b")
twice = legacy.encode(once)
if twice != once:
    defects.append(f"1 二次转义: encode({once!r}) -> {twice!r}")

# 缺陷 2：不完整实体静默原样输出，下游解析失败
out = legacy.decode("a &lt b")
if out == "a &lt b":
    defects.append(f"2 不完整实体静默放行: decode('a &lt b') -> {out!r}")

# 缺陷 3：数字实体越界/代理区未按规范拒绝
try:
    bad = legacy.decode("&#xD800;")
    defects.append(f"3 代理区码位产生非法字符: decode('&#xD800;') -> U+{ord(bad):04X}")
except ValueError:
    defects.append("3 越界码位抛出无位置信息的裸 ValueError")
try:
    legacy.decode("&#1114112;")
    defects.append("3 越界码位 U+110000 未被拒绝")
except ValueError as exc:
    if "position" not in str(exc):
        defects.append(f"3 越界报错不含位置与规范原因: {exc}")

# 缺陷 4：往返不能还原原文（encode 产出 &apos;，decode 不识别）
try:
    restored = legacy.decode(legacy.encode("it's"))
    if restored != "it's":
        defects.append(f"4 往返不一致: decode(encode(\"it's\")) -> {restored!r}")
except KeyError as exc:
    defects.append(f"4 往返失败: decode(encode(\"it's\")) 抛出 KeyError({exc})")

# 缺陷 5：实体名大小写敏感
try:
    upper = legacy.decode("&AMP;")
    if upper != "&":
        defects.append(f"5 大小写区别对待: decode('&AMP;') -> {upper!r}")
except KeyError as exc:
    defects.append(f"5 大小写区别对待: decode('&AMP;') 抛出 KeyError({exc})")

for line in defects:
    print("[复现]", line)
classes = {line.split(" ", 1)[0] for line in defects}
print(f"\n共复现 {len(classes)}/5 类缺陷")
sys.exit(0 if len(classes) == 5 else 1)
