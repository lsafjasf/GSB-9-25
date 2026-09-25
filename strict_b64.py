"""严格 Base64 编解码（RFC 4648）。

只使用标准库基础类型，不调用 base64、binascii 等自带编解码模块。
三个独立开关，绝不自动猜测：
  urlsafe          标准字母表 / URL 安全字母表（必须显式二选一）
  require_padding  解码时要求带填充 / 要求无填充
  allow_newlines   是否允许 \\n / \\r\\n 换行
"""

__all__ = ["B64DecodeError", "b64_encode", "b64_decode"]

_STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


class B64DecodeError(ValueError):
    """Base64 解码错误。position 为出错字符在输入中的下标（0 起）。"""

    def __init__(self, message, position=None):
        self.position = position
        if position is not None:
            message = "%s（位置 %d）" % (message, position)
        super().__init__(message)


def _build_pair_table(alphabet):
    # 12bit -> 两个字符。编码主循环每次查表产出 2 个字符，把循环开销减半。
    return tuple(alphabet[i] + alphabet[j] for i in range(64) for j in range(64))


def _build_dec_table(alphabet):
    t = [-1] * 256
    for v, c in enumerate(alphabet):
        t[ord(c)] = v
    return t


def _build_check_map(alphabet):
    # str.translate 用：合法字符映射为 None（删除），非法字符原样保留。
    # 这样可以用 C 速度一次性找出全部非法字符，合法输入零 Python 循环开销。
    return {ord(c): None for c in alphabet}


_PAIR_STD = _build_pair_table(_STD_ALPHABET)
_PAIR_URL = _build_pair_table(_URL_ALPHABET)
_DEC_STD = _build_dec_table(_STD_ALPHABET)
_DEC_URL = _build_dec_table(_URL_ALPHABET)
_CHECK_STD = _build_check_map(_STD_ALPHABET)
_CHECK_URL = _build_check_map(_URL_ALPHABET)


def b64_encode(data, *, urlsafe=False, padding=True):
    """编码为 Base64 字符串。默认标准字母表 + 带填充。"""
    alphabet = _URL_ALPHABET if urlsafe else _STD_ALPHABET
    pair = _PAIR_URL if urlsafe else _PAIR_STD
    mv = memoryview(data).cast("B")
    n = len(mv)
    parts = []
    ap = parts.append
    i = 0
    # 主循环：每 3 字节 -> 4 字符，分两次 12bit 查表。
    while i + 3 <= n:
        v = (mv[i] << 16) | (mv[i + 1] << 8) | mv[i + 2]
        ap(pair[v >> 12])
        ap(pair[v & 0xFFF])
        i += 3
    rem = n - i
    if rem == 1:
        b0 = mv[i]
        ap(alphabet[b0 >> 2])
        ap(alphabet[(b0 & 0x03) << 4])
        if padding:
            ap("=")
            ap("=")
    elif rem == 2:
        v = (mv[i] << 8) | mv[i + 1]
        ap(alphabet[v >> 10])
        ap(alphabet[(v >> 4) & 0x3F])
        ap(alphabet[(v & 0x0F) << 2])
        if padding:
            ap("=")
    return "".join(parts)


def b64_decode(text, *, urlsafe=False, require_padding=True, allow_newlines=False):
    """严格解码 Base64 字符串。

    非法字符、长度非法、填充错误、填充缺失、非零填充位均抛
    B64DecodeError 并给出位置，不做任何自动纠正。
    """
    if not isinstance(text, str):
        raise TypeError("b64_decode 需要 str 输入，得到 %s" % type(text).__name__)

    if "\n" in text or "\r" in text:
        if not allow_newlines:
            positions = [p for p in (text.find("\n"), text.find("\r")) if p >= 0]
            raise B64DecodeError("包含换行符（如需兼容请显式传 allow_newlines=True）",
                                 min(positions))
        text = text.replace("\r", "").replace("\n", "")

    name = "URL 安全" if urlsafe else "标准"
    dec = _DEC_URL if urlsafe else _DEC_STD
    check = _CHECK_URL if urlsafe else _CHECK_STD

    n = len(text)
    pad = 0
    first_eq = text.find("=")
    if first_eq >= 0:
        if not require_padding:
            raise B64DecodeError("无填充模式（require_padding=False）下不允许 '='", first_eq)
        for j in range(first_eq, n):
            if text[j] != "=":
                raise B64DecodeError("'=' 只能出现在末尾", j)
        pad = n - first_eq
        if pad > 2:
            raise B64DecodeError("填充字符过多（最多 2 个）", first_eq + 2)
        body = text[:first_eq]
    else:
        body = text

    m = len(body)
    # 先校验字符（错误定位优先指向非法字符本身），再校验长度。
    # C 速度校验：translate 删除合法字符，剩下的都是非法字符。
    bad = body.translate(check)
    if bad:
        badset = set(bad)
        for idx, c in enumerate(body):
            if c in badset:
                raise B64DecodeError(
                    "非法字符 %r（不属于%s字母表）" % (c, name), idx)

    if pad:
        if (m + pad) % 4 != 0:
            raise B64DecodeError("长度非法：含填充总长 %d 不是 4 的倍数" % (m + pad), n)
    else:
        if require_padding and n % 4 != 0:
            raise B64DecodeError("填充缺失：长度 %d 不是 4 的倍数" % n, n)
        if m % 4 == 1:
            raise B64DecodeError("长度非法：有效长度模 4 余 1，不可能由合法编码产生", m)

    # 规范性检查：末尾未使用的填充位必须为 0（RFC 4648 §3.5）。
    if m:
        last = dec[ord(body[m - 1])]
        if pad == 2 or (not pad and m % 4 == 2):
            if last & 0x0F:
                raise B64DecodeError("非零填充位：末字符低 4 位必须为 0", m - 1)
        elif pad == 1 or (not pad and m % 4 == 3):
            if last & 0x03:
                raise B64DecodeError("非零填充位：末字符低 2 位必须为 0", m - 1)

    out = bytearray((m * 6) // 8)
    w = 0
    i = 0
    # 主循环：每 4 字符 -> 3 字节，直接写入预分配的 bytearray，避免拼接。
    while i + 4 <= m:
        v = (dec[ord(body[i])] << 18) | (dec[ord(body[i + 1])] << 12) \
            | (dec[ord(body[i + 2])] << 6) | dec[ord(body[i + 3])]
        out[w] = (v >> 16) & 0xFF
        out[w + 1] = (v >> 8) & 0xFF
        out[w + 2] = v & 0xFF
        w += 3
        i += 4
    rem = m - i
    if rem == 2:
        v = (dec[ord(body[i])] << 6) | dec[ord(body[i + 1])]
        out[w] = (v >> 4) & 0xFF
    elif rem == 3:
        v = (dec[ord(body[i])] << 12) | (dec[ord(body[i + 1])] << 6) \
            | dec[ord(body[i + 2])]
        out[w] = (v >> 10) & 0xFF
        out[w + 1] = (v >> 2) & 0xFF
    return bytes(out)
