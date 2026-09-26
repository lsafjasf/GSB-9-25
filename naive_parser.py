"""现网版本（有缺陷）的 键=值 配置行解析器。

已知五类缺陷（见 reproduce.py）：
1. 值中的引号被当普通字符，遇空格即截断；
2. 反斜杠转义在单引号内也被吞掉；
3. 空值（KEY=）与值缺失（KEY）无法区分；
4. 引号内的 # 也被当作注释截掉；
5. 非法行只报 "parse failed"，没有行号/列位置。
"""

import re

_ESCAPE_RE = re.compile(r"\\(.)")


def parse_line(line, line_no=1):
    """解析一行，返回 (key, value)；空行/注释行返回 None。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if "=" in text:
        key, _, value = text.partition("=")
    else:
        # 缺陷 3：值缺失被静默当成空值
        key, value = text, ""
    key = key.strip()
    # 缺陷 4：不区分引号，# 一律当注释
    value = value.split("#", 1)[0].strip()
    if value:
        # 缺陷 1：引号被当普通字符，遇空白直接截断
        value = value.split()[0]
    # 粗暴剥掉首尾引号字符
    value = value.strip("'\"")
    # 缺陷 2：任何语境下都吞掉反斜杠
    value = _ESCAPE_RE.sub(r"\1", value)
    if not key:
        # 缺陷 5：错误信息不含行号与列位置
        raise ValueError("parse failed")
    return key, value


def parse_config(text):
    entries = []
    for line_no, line in enumerate(text.splitlines(), 1):
        result = parse_line(line, line_no)
        if result is not None:
            entries.append(result)
    return entries
