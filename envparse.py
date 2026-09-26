"""修复后的 键=值 配置行解析器（仅标准库）。

解析规则（与 PARSING_RULES.md 一致）：

- 行格式为 ``KEY=VALUE``；空白行与以 ``#`` 开头的行被跳过。
- 键：第一个 ``=`` 之前的部分，去掉首尾空白；为空则报错。
- 值：第一个 ``=`` 之后的部分，按下述引号/转义语义解析，
  除此之外不做任何改动（内部空格、分隔符、``=`` 都保留）。
- 单引号：内部不做任何转义，``\`` 与 ``"`` 都是普通字符，
  直到下一个 ``'`` 结束。
- 双引号：支持转义序列 ``\\  \"  \n  \t  \r  \$``；
  未列出的序列保留原样（如 ``\q`` 仍是 ``\q``）。
- 引号外：``\`` 转义紧随其后的任意字符（含 ``#``、空格、``=``、引号）。
- 注释：引号外的 ``#`` 若处于值的开头或前面是空白，则到行尾为注释；
  否则（如 ``a#b``）``#`` 是普通字符。引号内的 ``#`` 一律是普通字符。
- 引号外的首尾空白被忽略；引号内的空白原样保留。
- 空值与值缺失：``KEY=`` 得到空字符串（has_value=True）；
  ``KEY``（缺少 ``=``）默认报 missing_equals 错误，调用方可据此
  选择默认值；也可用 allow_bare_key=True 让其返回 has_value=False。
- 非法行（缺少 ``=``、引号未闭合、键为空、行尾孤立反斜杠）抛出
  ParseError，携带行号 line 与列位置 column（均从 1 开始）。
"""

from dataclasses import dataclass
from typing import List, Optional

__all__ = ["ParseError", "Entry", "parse_line", "parse_config"]

# 双引号内支持的转义序列
_DOUBLE_QUOTED_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "$": "$",
}

_WHITESPACE = " \t"


class ParseError(ValueError):
    """解析错误，携带行号与列位置（均从 1 开始）。

    kind 取值：missing_equals / empty_key / unterminated_quote /
    dangling_escape，调用方可据此区分原因并选择默认值。
    """

    def __init__(self, message, line, column, kind):
        self.message = message
        self.line = line
        self.column = column
        self.kind = kind
        super().__init__(f"line {line}, column {column}: {message} ({kind})")


@dataclass
class Entry:
    key: str
    value: Optional[str]  # has_value 为 False 时为 None
    has_value: bool
    line: int


def parse_line(line, line_no=1, allow_bare_key=False):
    """解析单行，返回 Entry；空白行/整行注释返回 None。

    allow_bare_key=True 时，缺少 ``=`` 的行返回
    Entry(value=None, has_value=False) 而不是抛错。
    """
    text = line.rstrip("\r\n")
    stripped = text.lstrip(_WHITESPACE)
    if not stripped or stripped.startswith("#"):
        return None

    eq = text.find("=")
    if eq == -1:
        if allow_bare_key:
            return Entry(key=stripped, value=None, has_value=False, line=line_no)
        raise ParseError(
            "missing '='; expected KEY=VALUE", line_no, len(text) + 1, "missing_equals"
        )

    key = text[:eq].strip(_WHITESPACE)
    if not key:
        raise ParseError("empty key before '='", line_no, eq + 1, "empty_key")

    value = _parse_value(text, eq + 1, line_no)
    return Entry(key=key, value=value, has_value=True, line=line_no)


def _parse_value(text, start, line_no):
    """解析 text[start:] 为值，应用引号/转义/注释规则。"""
    n = len(text)
    i = start
    while i < n and text[i] in _WHITESPACE:
        i += 1

    out = []
    significant_end = 0  # out 中最后一个“有效”字符之后的位置
    state = None  # None=引号外, "'"=单引号内, '"'=双引号内
    quote_col = None
    prev_allows_comment = True  # 值的开头或前一字符是空白

    while i < n:
        ch = text[i]
        if state is None:
            if ch == "'" or ch == '"':
                state = ch
                quote_col = i + 1
                prev_allows_comment = False
            elif ch == "\\":
                if i + 1 >= n:
                    raise ParseError(
                        "dangling backslash at end of line",
                        line_no,
                        i + 1,
                        "dangling_escape",
                    )
                out.append(text[i + 1])
                significant_end = len(out)
                prev_allows_comment = False
                i += 2
                continue
            elif ch == "#" and prev_allows_comment:
                break  # 行尾注释
            else:
                out.append(ch)
                if ch in _WHITESPACE:
                    prev_allows_comment = True
                else:
                    significant_end = len(out)
                    prev_allows_comment = False
        elif state == "'":
            # 单引号内不做任何转义
            if ch == "'":
                state = None
                significant_end = len(out)
                prev_allows_comment = False
            else:
                out.append(ch)
        else:  # 双引号内
            if ch == '"':
                state = None
                significant_end = len(out)
                prev_allows_comment = False
            elif ch == "\\":
                if i + 1 >= n:
                    raise ParseError(
                        "dangling backslash at end of line",
                        line_no,
                        i + 1,
                        "dangling_escape",
                    )
                nxt = text[i + 1]
                out.append(_DOUBLE_QUOTED_ESCAPES.get(nxt, "\\" + nxt))
                i += 2
                continue
            else:
                out.append(ch)
        i += 1

    if state is not None:
        raise ParseError(
            f"unterminated {state} quote", line_no, quote_col, "unterminated_quote"
        )
    return "".join(out[:significant_end])


def parse_config(text, allow_bare_key=False):
    """解析多行配置文本，返回 Entry 列表。"""
    entries: List[Entry] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        entry = parse_line(line, line_no, allow_bare_key=allow_bare_key)
        if entry is not None:
            entries.append(entry)
    return entries
