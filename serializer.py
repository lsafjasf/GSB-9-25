"""结构化数据序列化（格式 v2），用于缓存与日志。仅依赖标准库。

支持类型: None / bool / int(任意精度) / float / str / list / dict(str 键)。

设计要点（对应旧版五类缺陷的修复）:
  1. 字符串: UTF-8 长度前缀编码（S<字节数>:<原始字节>），不依赖转义，
     多字节字符与 \\、"、换行等任意字节序列均可无损往返。
  2. 整数: 十进制文本直写（I<digits>;），任意精度，不经 float。
  3. 共享引用: 编码端按 id() 建引用表，重复出现的容器写 R<n>;，
     解码端按先序编号重建，共享关系（is）保持不变。
  4. 循环引用: 由同一引用表确定性支持（见 FORMAT.md 的选择理由）；
     另有 MAX_DEPTH 深度上限兜底，任何输入都不会栈溢出。
  5. 解析错误: ParseError 携带字节偏移 offset 与期望结构 expected。

兼容性: loads() 自动识别旧版 v1 文本格式（JSON 子集）并只读兼容，
详见 FORMAT.md。
"""

from __future__ import annotations

import re

__all__ = ["dumps", "loads", "EncodeError", "ParseError", "FORMAT_VERSION", "MAX_DEPTH"]

FORMAT_VERSION = 2
MAX_DEPTH = 200  # 嵌套深度上限，编解码两侧一致，杜绝栈溢出

_TAG_NONE = ord("N")
_TAG_TRUE = ord("T")
_TAG_FALSE = ord("F")
_TAG_INT = ord("I")
_TAG_FLOAT = ord("D")
_TAG_STR = ord("S")
_TAG_LIST = ord("L")
_TAG_DICT = ord("M")
_TAG_REF = ord("R")

_NEW_TAG_BYTES = frozenset(b"NTFIDS LMR".replace(b" ", b""))
_TAG_NAMES = "N,T,F,I,D,S,L,M,R"


class EncodeError(TypeError):
    """对象图无法编码（不支持的类型 / 非 str 键 / 嵌套过深）。"""


class ParseError(ValueError):
    """反序列化失败。携带字节偏移与期望结构，便于定位。"""

    def __init__(self, offset, expected, found=None):
        self.offset = offset
        self.expected = expected
        self.found = found
        if found is None:
            message = "parse error at byte offset %d: expected %s, got end of input" % (
                offset, expected)
        else:
            message = "parse error at byte offset %d: expected %s, got %r" % (
                offset, expected, found)
        super().__init__(message)


# ---------------------------------------------------------------- 编码

def dumps(obj):
    """把对象图序列化为 bytes。共享/循环引用通过引用表保留。"""
    out = bytearray()
    _encode(obj, out, {}, 0)
    return bytes(out)


def _encode(obj, out, memo, depth):
    if depth > MAX_DEPTH:
        raise EncodeError("object graph nested deeper than %d levels" % MAX_DEPTH)
    if obj is None:
        out += b"N"
    elif obj is True:
        out += b"T"
    elif obj is False:
        out += b"F"
    elif isinstance(obj, int):
        out += b"I" + str(obj).encode("ascii") + b";"
    elif isinstance(obj, float):
        out += b"D" + repr(obj).encode("ascii") + b";"
    elif isinstance(obj, str):
        raw = obj.encode("utf-8")
        out += b"S" + str(len(raw)).encode("ascii") + b":" + raw
    elif isinstance(obj, (list, dict)):
        ref = memo.get(id(obj))
        if ref is not None:
            out += b"R" + str(ref).encode("ascii") + b";"
            return
        memo[id(obj)] = len(memo)  # 先序编号，与解码端 table 顺序一致
        if isinstance(obj, list):
            out += b"L" + str(len(obj)).encode("ascii") + b";"
            for item in obj:
                _encode(item, out, memo, depth + 1)
        else:
            out += b"M" + str(len(obj)).encode("ascii") + b";"
            for key, value in obj.items():
                if not isinstance(key, str):
                    raise EncodeError(
                        "dict keys must be str, got %s" % type(key).__name__)
                _encode(key, out, memo, depth + 1)
                _encode(value, out, memo, depth + 1)
    else:
        raise EncodeError("unsupported type: %s" % type(obj).__name__)


# ---------------------------------------------------------------- 解码

def loads(data):
    """反序列化。接受 bytes 或 str；自动识别 v2 二进制格式与 v1 旧文本格式。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("loads() expects bytes or str, got %s" % type(data).__name__)
    data = bytes(data)
    if not data:
        raise ParseError(0, "value tag (one of %s)" % _TAG_NAMES)
    if data[0] not in _NEW_TAG_BYTES:
        return _loads_legacy(data)  # v1 历史数据只读兼容
    parser = _Parser(data)
    value = parser.parse(0)
    if parser.pos != len(data):
        raise ParseError(parser.pos, "end of input",
                         data[parser.pos:parser.pos + 16])
    return value


class _Parser(object):
    __slots__ = ("data", "pos", "table")

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.table = []  # 引用表：先序编号的容器

    def parse(self, depth):
        if depth > MAX_DEPTH:
            raise ParseError(self.pos, "nesting depth <= %d" % MAX_DEPTH,
                             "deeper nesting")
        tag_pos = self.pos
        if tag_pos >= len(self.data):
            raise ParseError(tag_pos, "value tag (one of %s)" % _TAG_NAMES)
        tag = self.data[tag_pos]
        self.pos = tag_pos + 1
        if tag == _TAG_NONE:
            return None
        if tag == _TAG_TRUE:
            return True
        if tag == _TAG_FALSE:
            return False
        if tag == _TAG_INT:
            return self._parse_int()
        if tag == _TAG_FLOAT:
            return self._parse_float()
        if tag == _TAG_STR:
            return self._parse_str()
        if tag == _TAG_LIST:
            return self._parse_list(depth)
        if tag == _TAG_DICT:
            return self._parse_dict(depth)
        if tag == _TAG_REF:
            return self._parse_ref()
        raise ParseError(tag_pos, "value tag (one of %s)" % _TAG_NAMES,
                         bytes([tag]))

    def _read_token(self, delim, what):
        start = self.pos
        idx = self.data.find(delim, start)
        if idx < 0:
            raise ParseError(start, "%s terminated by %r" % (what, delim))
        self.pos = idx + 1
        return self.data[start:idx], start

    def _parse_int(self):
        token, start = self._read_token(b";", "integer")
        digits = token[1:] if token[:1] == b"-" else token
        if not digits or not digits.isdigit():
            raise ParseError(start, "integer digits", token)
        return int(token)

    def _parse_float(self):
        token, start = self._read_token(b";", "float")
        try:
            return float(token.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            raise ParseError(start, "float literal", token) from None

    def _parse_str(self):
        token, start = self._read_token(b":", "string byte length")
        if not token.isdigit():
            raise ParseError(start, "string byte length (decimal)", token)
        size = int(token)
        remaining = len(self.data) - self.pos
        if remaining < size:
            raise ParseError(self.pos, "%d bytes of string payload" % size,
                             "%d bytes remaining" % remaining)
        raw = self.data[self.pos:self.pos + size]
        self.pos += size
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParseError(self.pos - size + exc.start, "valid UTF-8",
                             raw[exc.start:exc.start + 1]) from None

    def _parse_count(self, what):
        token, start = self._read_token(b";", what + " element count")
        if not token.isdigit():
            raise ParseError(start, what + " element count (decimal)", token)
        return int(token)

    def _parse_list(self, depth):
        count = self._parse_count("list")
        result = []
        self.table.append(result)  # 先注册再填内容，循环引用可指向自身
        for _ in range(count):
            result.append(self.parse(depth + 1))
        return result

    def _parse_dict(self, depth):
        count = self._parse_count("dict")
        result = {}
        self.table.append(result)
        for _ in range(count):
            key = self.parse(depth + 1)
            if not isinstance(key, str):
                raise ParseError(self.pos, "str dict key", type(key).__name__)
            result[key] = self.parse(depth + 1)
        return result

    def _parse_ref(self):
        token, start = self._read_token(b";", "reference id")
        if not token.isdigit():
            raise ParseError(start, "reference id (decimal)", token)
        ref = int(token)
        if ref >= len(self.table):
            raise ParseError(start, "reference id < %d (already defined)" % len(self.table),
                             ref)
        return self.table[ref]


# ---------------------------------------------------------------- v1 旧格式只读兼容

_LEGACY_NUMBER_RE = re.compile(rb"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_LEGACY_ESCAPES = {ord('"'): '"', ord("\\"): "\\", ord("/"): "/",
                   ord("n"): "\n", ord("t"): "\t", ord("r"): "\r",
                   ord("b"): "\b", ord("f"): "\f", ord("u"): None}
_HEX = re.compile(rb"[0-9a-fA-F]{4}")


def _loads_legacy(data):
    """解析旧版 v1 文本格式（JSON 子集）。历史数据只读入口。

    注意：旧实现写出的数字一律是 float，此处按原样还原；
    写出时已损坏的字符串/大整数无法恢复（见 FORMAT.md 兼容性边界）。
    """
    parser = _LegacyParser(data)
    value = parser.parse_value(0)
    parser.skip_ws()
    if parser.pos != len(data):
        raise ParseError(parser.pos, "end of input",
                         data[parser.pos:parser.pos + 16])
    return value


class _LegacyParser(object):
    __slots__ = ("data", "pos")

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def skip_ws(self):
        data = self.data
        while self.pos < len(data) and data[self.pos] in b" \t\r\n":
            self.pos += 1

    def parse_value(self, depth):
        if depth > MAX_DEPTH:
            raise ParseError(self.pos, "nesting depth <= %d" % MAX_DEPTH,
                             "deeper nesting")
        self.skip_ws()
        if self.pos >= len(self.data):
            raise ParseError(self.pos, "value (string/number/array/object/literal)")
        ch = self.data[self.pos]
        if ch == ord('"'):
            return self.parse_string()
        if ch == ord("["):
            return self.parse_array(depth)
        if ch == ord("{"):
            return self.parse_object(depth)
        for literal, value in ((b"null", None), (b"true", True), (b"false", False)):
            if self.data.startswith(literal, self.pos):
                self.pos += len(literal)
                return value
        match = _LEGACY_NUMBER_RE.match(self.data, self.pos)
        if match:
            self.pos = match.end()
            token = match.group()
            if b"." in token or b"e" in token or b"E" in token:
                return float(token)
            return int(token)
        raise ParseError(self.pos, "value (string/number/array/object/literal)",
                         bytes([ch]))

    def parse_string(self):
        self.pos += 1  # 开引号
        out = []
        data = self.data
        while True:
            if self.pos >= len(data):
                raise ParseError(self.pos, "closing quote")
            ch = data[self.pos]
            if ch == ord('"'):
                self.pos += 1
                return "".join(out)
            if ch == ord("\\"):
                esc_pos = self.pos
                self.pos += 1
                if self.pos >= len(data):
                    raise ParseError(esc_pos, "escape sequence")
                esc = data[self.pos]
                self.pos += 1
                if esc == ord("u"):
                    hex_digits = _HEX.match(data, self.pos)
                    if not hex_digits:
                        raise ParseError(self.pos, "4 hex digits after \\u",
                                         data[self.pos:self.pos + 4])
                    out.append(chr(int(hex_digits.group(), 16)))
                    self.pos = hex_digits.end()
                elif esc in _LEGACY_ESCAPES:
                    out.append(_LEGACY_ESCAPES[esc])
                else:
                    raise ParseError(esc_pos, "valid escape (\\\" \\\\ \\/ \\n \\t \\r \\b \\f \\uXXXX)",
                                     bytes([esc]))
            else:
                out.append(chr(ch))
                self.pos += 1

    def parse_array(self, depth):
        self.pos += 1
        out = []
        self.skip_ws()
        if self.pos < len(self.data) and self.data[self.pos] == ord("]"):
            self.pos += 1
            return out
        while True:
            out.append(self.parse_value(depth + 1))
            self.skip_ws()
            if self.pos >= len(self.data):
                raise ParseError(self.pos, "',' or ']'")
            ch = self.data[self.pos]
            self.pos += 1
            if ch == ord(","):
                continue
            if ch == ord("]"):
                return out
            raise ParseError(self.pos - 1, "',' or ']'", bytes([ch]))

    def parse_object(self, depth):
        self.pos += 1
        out = {}
        self.skip_ws()
        if self.pos < len(self.data) and self.data[self.pos] == ord("}"):
            self.pos += 1
            return out
        while True:
            self.skip_ws()
            if self.pos >= len(self.data) or self.data[self.pos] != ord('"'):
                raise ParseError(self.pos, "object key (string)")
            key = self.parse_string()
            self.skip_ws()
            if self.pos >= len(self.data) or self.data[self.pos] != ord(":"):
                raise ParseError(self.pos, "':'")
            self.pos += 1
            out[key] = self.parse_value(depth + 1)
            self.skip_ws()
            if self.pos >= len(self.data):
                raise ParseError(self.pos, "',' or '}'")
            ch = self.data[self.pos]
            self.pos += 1
            if ch == ord(","):
                continue
            if ch == ord("}"):
                return out
            raise ParseError(self.pos - 1, "',' or '}'", bytes([ch]))
