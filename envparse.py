"""修复后的环境变量 / 配置行解析工具（仅依赖标准库）。

解析规则（与 PARSING_RULES.md 一致）：

行级规则
- 空行、纯空白行、以 `#` 开头（允许前导空白）的行为注释行，跳过。
- 配置行形如 `键=值`，以第一个 `=` 分隔键与值；键两端空白被忽略。
- 缺少 `=`、键为空、引号未闭合均为非法行，抛出 ParseError，
  携带 1 起始的行号 (line) 与列号 (col)。

值规则
- 值取 `=` 之后的原始内容，除引号与转义语义外不做任何改动：
  前后空格、内部分隔符、其余的 `=` 全部保留。
- 单引号：内部完全字面，不做任何转义，直到下一个 `'`。
- 双引号：支持转义序列 \\\\ \\" \\n \\t \\r；其余 \\x 原样保留（含反斜杠）。
- 引号字符本身是语法，不进入结果；引号片段与字面片段直接拼接。
- 引号可嵌套：一种引号内的另一种引号是普通字符。
- 注释：`#` 在引号外、且位于行首或前一字符是空格/制表符时，为注释起始，
  其后的内容被丢弃（`#` 之前的空白仍属于值）；其余位置的 `#` 是普通字符。
- 单引号与双引号之外，反斜杠是普通字符。

空值与值缺失
- `KEY=` 解析为 Entry(value="", has_value=True)，表示显式空值。
- 键不存在时 Config.get_entry() 返回 None、Config.get() 返回调用方给的默认值，
  调用方可据此区分“空值”与“缺失”并选择默认值。
"""

from dataclasses import dataclass

__all__ = ["Entry", "ParseError", "Config", "parse_line", "parse_text", "parse_file"]


class ParseError(ValueError):
    """解析错误，携带 1 起始的行号与列号。"""

    def __init__(self, message, line, col):
        self.message = message
        self.line = line
        self.col = col
        super().__init__(f"line {line}, col {col}: {message}")


@dataclass(frozen=True)
class Entry:
    key: str
    value: str
    has_value: bool = True  # 行中存在 `=` 时为 True；键缺失时根本不会产生 Entry


_DOUBLE_QUOTE_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "n": "\n",
    "t": "\t",
    "r": "\r",
}


def parse_line(line, lineno=1):
    """解析单行。返回 Entry；空行/注释行返回 None；非法行抛 ParseError。"""
    text = line.rstrip("\r\n")
    stripped = text.lstrip(" \t")
    if not stripped or stripped.startswith("#"):
        return None
    eq = text.find("=")
    if eq == -1:
        raise ParseError("missing '='", lineno, len(text) + 1)
    key = text[:eq].strip(" \t")
    if not key:
        raise ParseError("empty key", lineno, eq + 1)
    value = _parse_value(text[eq + 1 :], lineno, eq + 2)
    return Entry(key=key, value=value, has_value=True)


def _parse_value(raw, lineno, base_col):
    """解析 `=` 之后的原始值。base_col 为 raw[0] 在原行中的 1 起始列号。"""
    out = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "#" and (i == 0 or raw[i - 1] in " \t"):
            break  # 注释起始；已输出的前置空白保留在值中
        if ch == "'":
            end = raw.find("'", i + 1)
            if end == -1:
                raise ParseError("unterminated single quote", lineno, base_col + i)
            out.append(raw[i + 1 : end])  # 单引号内完全字面，无转义
            i = end + 1
        elif ch == '"':
            buf = []
            j = i + 1
            closed = False
            while j < n:
                cj = raw[j]
                if cj == "\\" and j + 1 < n:
                    nxt = raw[j + 1]
                    if nxt in _DOUBLE_QUOTE_ESCAPES:
                        buf.append(_DOUBLE_QUOTE_ESCAPES[nxt])
                    else:
                        buf.append("\\" + nxt)  # 未识别转义：原样保留
                    j += 2
                elif cj == '"':
                    closed = True
                    j += 1
                    break
                else:
                    buf.append(cj)
                    j += 1
            if not closed:
                raise ParseError("unterminated double quote", lineno, base_col + i)
            out.append("".join(buf))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class Config:
    """解析结果。保持键的插入顺序；重复键后者覆盖前者。"""

    def __init__(self, entries):
        self._entries = dict(entries)

    def __contains__(self, key):
        return key in self._entries

    def __len__(self):
        return len(self._entries)

    def keys(self):
        return self._entries.keys()

    def items(self):
        return self._entries.items()

    def get_entry(self, key):
        """返回 Entry；键缺失时返回 None（与空值 Entry(value="") 相区分）。"""
        return self._entries.get(key)

    def get(self, key, default=None):
        """键缺失返回 default；键存在时返回其值（空值返回 "" 而非 default）。"""
        entry = self._entries.get(key)
        return default if entry is None else entry.value


def parse_text(text):
    """解析多行文本为 Config；任一非法行抛 ParseError（含行号与列号）。"""
    entries = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        entry = parse_line(line, lineno)
        if entry is not None:
            entries[entry.key] = entry
    return Config(entries)


def parse_file(path, encoding="utf-8"):
    with open(path, "r", encoding=encoding) as fh:
        return parse_text(fh.read())
