"""旧版（有缺陷）的配置行解析器，仅用于复现五类现网问题。

缺陷清单：
1. 引号被当普通字符，值在第一个空白处被截断（"a b" -> '"a'）。
2. 反斜杠被无条件删除，转义序列丢失。
3. `KEY=`（空值）与 `KEY`（值缺失）都返回 ""，无法区分。
4. `#` 出现在引号内也会被当作注释截掉。
5. 非法行只抛出 "parse failed"，没有行号与列位置。
"""


def parse_line(line):
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if "=" not in text:
        # 缺陷 3：值缺失被当作空值处理，调用方无法区分
        return text, ""
    key, _, value = text.partition("=")
    # 缺陷 4：不识别引号，`#` 一律当注释起始
    value = value.split("#", 1)[0]
    # 缺陷 1：引号是普通字符，值在空白处被截断
    value = value.split()[0] if value.split() else ""
    # 缺陷 2：反斜杠被直接删除，转义丢失
    value = value.replace("\\", "")
    return key.strip(), value


def parse_text(text):
    result = {}
    for line in text.splitlines():
        item = parse_line(line)
        if item is None:
            continue
        key, value = item
        if not key:
            # 缺陷 5：只报 "parse failed"，无行号、无列位置
            raise ValueError("parse failed")
        result[key] = value
    return result
