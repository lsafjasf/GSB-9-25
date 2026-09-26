"""旧版结构化数据序列化实现（v1，JSON 子集文本格式）。

!! 已下线，仅保留用于回归对照。存在五类已知缺陷，禁止在生产使用 !!
  缺陷1: 字符串未转义反斜杠；非 ASCII 写成 \\uXXXX 但解码端不还原 -> 内容改变
  缺陷2: 所有数字经 float() 序列化 -> 大整数精度丢失
  缺陷3: 无引用表 -> 共享引用被展开成多份拷贝
  缺陷4: 无循环检测 -> 循环引用无限递归直至 RecursionError
  缺陷5: 所有解析错误统一报 "parse failed" -> 无法定位
"""

import re

__all__ = ["dumps", "loads"]

_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def dumps(obj):
    return _encode(obj)


def _encode(obj):
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)):
        return repr(float(obj))  # 缺陷2: 整数被强制转成 float
    if isinstance(obj, str):
        out = []
        for ch in obj:
            if ch == '"':
                out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ord(ch) > 127:
                out.append("\\u%04x" % ord(ch))  # 缺陷1: 解码端不认识 \uXXXX
            else:
                out.append(ch)  # 缺陷1: 反斜杠原样输出，解码端会误吃
        return '"' + "".join(out) + '"'
    if isinstance(obj, (list, tuple)):
        # 缺陷3/4: 无 memo，共享引用被复制、循环引用无限递归
        return "[" + ",".join(_encode(item) for item in obj) + "]"
    if isinstance(obj, dict):
        return "{" + ",".join(
            _encode(str(key)) + ":" + _encode(value) for key, value in obj.items()
        ) + "}"
    raise TypeError("unsupported type: %s" % type(obj).__name__)


def loads(text):
    try:
        value, pos = _parse_value(text, 0)
        if text[pos:].strip():
            raise ValueError("trailing data")
        return value
    except Exception as exc:
        # 缺陷5: 吞掉所有上下文，只报一句「解析失败」
        raise ValueError("parse failed") from exc


_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}


def _skip_ws(s, i):
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _parse_string(s, i):
    i += 1  # 跳过开引号
    out = []
    while True:
        ch = s[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch == "\\":
            nxt = s[i + 1]
            # 缺陷1: 未知转义（含 \uXXXX）被静默改写为字面字符
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1


def _parse_value(s, i):
    i = _skip_ws(s, i)
    ch = s[i]
    if ch == '"':
        return _parse_string(s, i)
    if ch == "[":
        i = _skip_ws(s, i + 1)
        out = []
        if s[i] == "]":
            return out, i + 1
        while True:
            value, i = _parse_value(s, i)
            out.append(value)
            i = _skip_ws(s, i)
            if s[i] == ",":
                i += 1
                continue
            if s[i] == "]":
                return out, i + 1
            raise ValueError("bad array")
    if ch == "{":
        i = _skip_ws(s, i + 1)
        out = {}
        if s[i] == "}":
            return out, i + 1
        while True:
            i = _skip_ws(s, i)
            key, i = _parse_string(s, i)
            i = _skip_ws(s, i)
            if s[i] != ":":
                raise ValueError("bad object")
            value, i = _parse_value(s, i + 1)
            out[key] = value
            i = _skip_ws(s, i)
            if s[i] == ",":
                i += 1
                continue
            if s[i] == "}":
                return out, i + 1
            raise ValueError("bad object")
    if s.startswith("null", i):
        return None, i + 4
    if s.startswith("true", i):
        return True, i + 4
    if s.startswith("false", i):
        return False, i + 5
    match = _NUMBER_RE.match(s, i)
    if match:
        return float(match.group()), match.end()  # 缺陷2: 数字一律按 float 解析
    raise ValueError("bad value")
