"""字符实体编解码工具（修复版，仅依赖标准库）。

策略说明（全流程统一）：
- strict=True（默认）：非法或不完整实体一律拒绝，抛出 EntityError，
  错误信息包含字符位置与原因；encode 与 decode 行为一致。
- strict=False：非法或不完整实体一律原样保留，encode 与 decode 行为一致。
- 裸露的 `&`（后随空白、标点或结尾，不构成实体尝试）不属于实体：
  encode 时转义为 `&amp;`，decode 时按普通字符原样通过，两种模式一致。

可断言性质：
- 幂等：对任意文本 s，encode(encode(s)) == encode(s)。
- 互逆：对不含实体样片段的文本 s，decode(encode(s)) == s；
  对合法实体文本 t，decode(encode(decode(t))) == decode(t)。
- 实体名大小写不敏感：`&AMP;`、`&Amp;`、`&amp;` 解码结果相同；
  编码输出统一使用小写规范形。
"""

from __future__ import annotations

import re
from html.entities import html5

__all__ = ["EntityError", "encode", "decode"]

MAX_CODE_POINT = 0x10FFFF
SURROGATE_MIN = 0xD800
SURROGATE_MAX = 0xDFFF

# 规范实体名表：键统一小写，实现大小写不敏感匹配。
_NAMED: dict[str, str] = {}
for _key, _char in html5.items():
    if _key.endswith(";"):
        _NAMED.setdefault(_key[:-1].lower(), _char)

# 完整合法实体：&#十进制; / &#x十六进制; / &名称;
_ENTITY_RE = re.compile(r"&(#(?:[xX][0-9A-Fa-f]+|[0-9]+)|[A-Za-z][A-Za-z0-9]*);")
# encode 需要处理的字符
_SPECIAL_RE = re.compile(r"[&<>\"']")
# 实体尝试片段：& 后随可选 # 与字母数字，可带结束分号
_ATTEMPT_RE = re.compile(r"&#?[A-Za-z0-9]*;?")

_ENCODE_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;"}


class EntityError(ValueError):
    """非法或不完整实体。position 为 0 起始的字符偏移，reason 为原因。"""

    def __init__(self, position: int, reason: str):
        self.position = position
        self.reason = reason
        super().__init__(f"position {position}: {reason}")


def _looks_like_entity_attempt(text: str, pos: int) -> bool:
    """text[pos] == '&' 时，判断其后是否像一次实体书写尝试。"""
    if pos + 1 >= len(text):
        return False
    nxt = text[pos + 1]
    return nxt == "#" or (nxt.isascii() and nxt.isalpha())


def _attempt_end(text: str, pos: int) -> int:
    return _ATTEMPT_RE.match(text, pos).end()


def _malformed_reason(text: str, pos: int) -> str:
    token = text[pos : _attempt_end(text, pos)]
    if token.endswith(";") and len(token) > 2:
        body = token[1:-1]
        if body.startswith("#"):
            return f"数字实体 {token!r} 格式非法（应为 &#十进制; 或 &#x十六进制;）"
        return f"未知实体名 {token!r}"
    return f"不完整实体 {token!r}：缺少结束分号 ';'"


def _decode_body(body: str, pos: int) -> str:
    """解码实体主体（不含 & 与 ;），非法时抛出 EntityError。"""
    if body.startswith("#"):
        if len(body) >= 2 and body[1] in "xX":
            digits, base = body[2:], 16
        else:
            digits, base = body[1:], 10
        if not digits:
            raise EntityError(pos, f"数字实体 '&{body};' 缺少数字")
        value = int(digits, base)
        if value > MAX_CODE_POINT:
            raise EntityError(
                pos,
                f"数字实体 '&#{body[1:]};' 的码位 U+{value:04X} "
                f"超出有效范围 U+0000–U+{MAX_CODE_POINT:04X}",
            )
        if SURROGATE_MIN <= value <= SURROGATE_MAX:
            raise EntityError(
                pos,
                f"数字实体 '&#{body[1:]};' 的码位 U+{value:04X} 位于代理区 "
                f"U+{SURROGATE_MIN:04X}–U+{SURROGATE_MAX:04X}，不是合法标量值",
            )
        return chr(value)
    char = _NAMED.get(body.lower())
    if char is None:
        raise EntityError(pos, f"未知实体名 '&{body};'")
    return char


def encode(text: str, *, strict: bool = True) -> str:
    """把特殊字符编码为实体；已是合法实体的片段原样保留（幂等）。

    strict=True：非法/不完整的实体尝试抛出 EntityError；
    strict=False：非法/不完整的实体尝试原样保留。
    """
    out: list[str] = []
    cursor = 0
    for match in _SPECIAL_RE.finditer(text):
        start = match.start()
        ch = match.group(0)
        if ch != "&":
            out.append(text[cursor:start])
            out.append(_ENCODE_MAP[ch])
            cursor = match.end()
            continue
        entity = _ENTITY_RE.match(text, start)
        if entity is not None:
            try:
                _decode_body(entity.group(1), start)
            except EntityError:
                if strict:
                    raise
                out.append(text[cursor : entity.end()])  # 原样保留
            else:
                out.append(text[cursor : entity.end()])  # 合法实体原样保留
            cursor = entity.end()
            continue
        if _looks_like_entity_attempt(text, start):
            if strict:
                raise EntityError(start, _malformed_reason(text, start))
            end = _attempt_end(text, start)
            out.append(text[cursor:end])  # 原样保留
            cursor = end
            continue
        out.append(text[cursor:start])
        out.append("&amp;")  # 裸露的 & 一律转义
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out)


def decode(text: str, *, strict: bool = True) -> str:
    """把实体解码为字符；裸露的 & 按普通字符通过。

    strict=True：非法/不完整的实体尝试抛出 EntityError；
    strict=False：非法/不完整的实体尝试原样保留。
    """
    out: list[str] = []
    cursor = 0
    i = text.find("&")
    while i != -1:
        entity = _ENTITY_RE.match(text, i)
        if entity is not None:
            try:
                decoded = _decode_body(entity.group(1), i)
            except EntityError:
                if strict:
                    raise
                out.append(text[cursor : entity.end()])  # 原样保留
            else:
                out.append(text[cursor:i])
                out.append(decoded)
            cursor = entity.end()
        elif _looks_like_entity_attempt(text, i):
            if strict:
                raise EntityError(i, _malformed_reason(text, i))
            end = _attempt_end(text, i)
            out.append(text[cursor:end])  # 原样保留
            cursor = end
        # 其余情况为裸露的 &，两种模式均按普通字符处理
        i = text.find("&", max(cursor, i + 1))
    out.append(text[cursor:])
    return "".join(out)
