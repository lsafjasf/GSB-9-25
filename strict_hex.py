"""严格十六进制编解码。

只使用标准库基础类型，不调用 binascii、bytes.hex、bytes.fromhex 等
自带编解码功能。编码输出固定小写；解码接受大小写输入，默认拒绝 0x 前缀。
"""

__all__ = ["HexDecodeError", "hex_encode", "hex_decode"]

_HEX_CHARS = "0123456789abcdef"

# 编码表：字节值 -> 两个小写字符。用列表推导一次查表，避免逐字符拼接。
_ENC_TABLE = tuple(_HEX_CHARS[i >> 4] + _HEX_CHARS[i & 0xF] for i in range(256))

# 解码表：字符码点 -> 半字节值，非法字符为 -1。大小写均接受。
_DEC_TABLE = [-1] * 256
for _v, _c in enumerate(_HEX_CHARS):
    _DEC_TABLE[ord(_c)] = _v
    _DEC_TABLE[ord(_c.upper())] = _v


class HexDecodeError(ValueError):
    """十六进制解码错误。position 为出错字符在输入中的下标（0 起）。"""

    def __init__(self, message, position=None):
        self.position = position
        if position is not None:
            message = "%s（位置 %d）" % (message, position)
        super().__init__(message)


def hex_encode(data):
    """把 bytes-like 编码为小写十六进制字符串。"""
    mv = memoryview(data).cast("B")
    # 一次 join，避免 += 造成的反复重分配（CPython 下也避免依赖其内部优化）。
    return "".join([_ENC_TABLE[b] for b in mv])


def hex_decode(text, *, allow_prefix=False):
    """严格解码十六进制字符串。

    - 输入大小写均可；
    - 默认拒绝 "0x"/"0X" 前缀，allow_prefix=True 时允许并剥离；
    - 奇数长度、非法字符均抛 HexDecodeError 并给出位置。
    """
    if not isinstance(text, str):
        raise TypeError("hex_decode 需要 str 输入，得到 %s" % type(text).__name__)
    if text[:2] in ("0x", "0X"):
        if not allow_prefix:
            raise HexDecodeError("不允许 '0x' 前缀（如需兼容请显式传 allow_prefix=True）", 0)
        text = text[2:]
    n = len(text)
    if n % 2:
        raise HexDecodeError("奇数长度 %d，无法按字节对齐" % n, n)
    out = bytearray(n // 2)
    dec = _DEC_TABLE
    for i in range(0, n, 2):
        o_hi = ord(text[i])
        hi = dec[o_hi] if o_hi < 256 else -1
        if hi < 0:
            raise HexDecodeError("非法字符 %r" % text[i], i)
        o_lo = ord(text[i + 1])
        lo = dec[o_lo] if o_lo < 256 else -1
        if lo < 0:
            raise HexDecodeError("非法字符 %r" % text[i + 1], i + 1)
        out[i >> 1] = (hi << 4) | lo
    return bytes(out)
