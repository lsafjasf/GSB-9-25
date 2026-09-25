"""Strict hexadecimal and Base64 codec library.

Pure Python 3, standard library only. Deliberately does NOT use any
built-in codec helpers (base64, binascii, codecs, bytes.hex, int(x, 16),
bytes.fromhex, ...); every table and bit operation is implemented here.

Design contract
---------------
Hex:
  * encode: fixed lowercase output.
  * decode: accepts upper/lowercase digits; odd length, illegal characters
    and (by default) a "0x"/"0X" prefix are hard errors carrying the exact
    0-based position. `allow_prefix=True` opts into stripping one prefix.

Base64:
  * `alphabet`: "standard" (RFC 4648 +/) or "urlsafe" (-_). Never guessed;
    a character from the other alphabet is an error at its position.
  * `padding`: "require" (must be present and correct), "forbid" (no '='
    allowed), "optional" (either, but if present it must be exact).
  * `allow_newlines`: when False, CR/LF are illegal characters; when True
    they are stripped before decoding (positions then refer to the
    stripped text). No other whitespace is ever tolerated.
  * Decoding is strict: illegal characters, impossible lengths (len % 4
    == 1), misplaced/missing/excess padding and non-canonical trailing
    bits all raise CodecError with a position. Nothing is auto-corrected.

All positions are 0-based character indices.
"""

__all__ = ["CodecError", "hex_encode", "hex_decode", "b64_encode", "b64_decode"]


class CodecError(ValueError):
    """Raised on any decode violation. `position` is a 0-based index."""

    def __init__(self, message, position=None):
        self.position = position
        if position is not None:
            message = "%s (at position %d)" % (message, position)
        super().__init__(message)


# --------------------------------------------------------------------------
# Hex
# --------------------------------------------------------------------------

_HEX_DIGITS = "0123456789abcdef"
_HEX_ENC_TABLE = [_HEX_DIGITS[b >> 4] + _HEX_DIGITS[b & 0x0F] for b in range(256)]

_HEX_DEC_TABLE = [-1] * 256
for _i, _c in enumerate("0123456789abcdef"):
    _HEX_DEC_TABLE[ord(_c)] = _i
for _i, _c in enumerate("0123456789ABCDEF"):
    _HEX_DEC_TABLE[ord(_c)] = _i


def hex_encode(data):
    """bytes -> lowercase hex str."""
    data = bytes(data)
    # map() over a precomputed 256-entry table: one C-level loop, no
    # Python-level string concatenation (which would be O(n^2)).
    return "".join(map(_HEX_ENC_TABLE.__getitem__, data))


def hex_decode(text, *, allow_prefix=False):
    """hex str -> bytes. Strict; see module docstring for the contract."""
    if not isinstance(text, str):
        raise TypeError("hex_decode expects str, got %s" % type(text).__name__)
    offset = 0
    if text[:2] in ("0x", "0X"):
        if not allow_prefix:
            raise CodecError("'0x' prefix not allowed (pass allow_prefix=True)", 0)
        text = text[2:]
        offset = 2
    n = len(text)
    if n % 2:
        # Position of the dangling final nibble.
        raise CodecError("odd number of hex digits", offset + n - 1)
    ords = [ord(c) for c in text]
    vals = [_HEX_DEC_TABLE[o] if o < 256 else -1 for o in ords]
    if -1 in vals:
        bad = vals.index(-1)
        raise CodecError("invalid hex character %r" % text[bad], offset + bad)
    # bytearray from a generator: single allocation, no += on bytes.
    return bytes(bytearray((vals[i] << 4) | vals[i + 1] for i in range(0, n, 2)))


# --------------------------------------------------------------------------
# Base64
# --------------------------------------------------------------------------

_ALPHABETS = {
    "standard": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
    "urlsafe": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_",
}

# 12-bit -> two output characters, so the encode loop emits 2 chars per
# lookup instead of 1 (halves the number of list appends).
_ENC_PAIR_TABLES = {
    name: [a[i >> 6] + a[i & 0x3F] for i in range(4096)]
    for name, a in _ALPHABETS.items()
}

_DEC_TABLES = {}
for _name, _alpha in _ALPHABETS.items():
    _t = [-1] * 256
    for _i, _c in enumerate(_alpha):
        _t[ord(_c)] = _i
    _DEC_TABLES[_name] = _t


def _check_alphabet(alphabet):
    if alphabet not in _ALPHABETS:
        raise ValueError("alphabet must be 'standard' or 'urlsafe', got %r" % (alphabet,))


def b64_encode(data, *, alphabet="standard", padding=True):
    """bytes -> Base64 str using the requested alphabet (never guessed)."""
    _check_alphabet(alphabet)
    data = bytes(data)
    alpha = _ALPHABETS[alphabet]
    pair = _ENC_PAIR_TABLES[alphabet]
    n = len(data)
    full = n - (n % 3)
    out = []
    append = out.append
    for i in range(0, full, 3):
        x = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
        append(pair[x >> 12])
        append(pair[x & 0xFFF])
    rem = n - full
    if rem == 1:
        x = data[full] << 4  # 6+2 bits
        append(alpha[(x >> 6) & 0x3F])
        append(alpha[x & 0x3F])
        if padding:
            append("==")
    elif rem == 2:
        x = (data[full] << 10) | (data[full + 1] << 2)  # 6+6+4 bits
        append(alpha[(x >> 12) & 0x3F])
        append(alpha[(x >> 6) & 0x3F])
        append(alpha[x & 0x3F])
        if padding:
            append("=")
    return "".join(out)


def b64_decode(text, *, alphabet="standard", padding="require", allow_newlines=False):
    """Base64 str -> bytes. Strict; see module docstring for the contract."""
    _check_alphabet(alphabet)
    if padding not in ("require", "forbid", "optional"):
        raise ValueError("padding must be 'require', 'forbid' or 'optional'")
    if not isinstance(text, str):
        raise TypeError("b64_decode expects str, got %s" % type(text).__name__)

    if allow_newlines:
        text = text.replace("\r", "").replace("\n", "")
    n = len(text)
    table = _DEC_TABLES[alphabet]

    first_pad = text.find("=")
    if first_pad == -1:
        pad = 0
        data_len = n
    else:
        if padding == "forbid":
            raise CodecError("padding not allowed (padding='forbid')", first_pad)
        for k in range(first_pad, n):
            if text[k] != "=":
                raise CodecError("padding character '=' in the middle of the data", first_pad)
        pad = n - first_pad
        if pad > 2:
            raise CodecError("too much padding (more than 2 '=' characters)", first_pad + 2)
        if n % 4 != 0:
            raise CodecError("padded length %d is not a multiple of 4" % n, n - 1)
        data_len = first_pad

    # Validate alphabet characters first: an illegal character is a more
    # precise diagnosis than any length/padding complaint.
    vals = []
    append_val = vals.append
    for i in range(data_len):
        o = ord(text[i])
        v = table[o] if o < 256 else -1
        if v < 0:
            raise CodecError(
                "invalid character %r for alphabet %r" % (text[i], alphabet), i)
        append_val(v)

    if pad == 0:
        if n % 4 == 1:
            raise CodecError("impossible Base64 length (length % 4 == 1)", n - 1)
        if padding == "require" and n % 4 != 0:
            raise CodecError("missing padding (length %d is not a multiple of 4)" % n, n)

    # Canonical-form check: the unused low bits of the last quantum must be
    # zero, otherwise two different strings would decode to the same bytes.
    rem = data_len % 4
    if rem == 2 and vals and vals[-1] & 0x0F:
        raise CodecError("non-canonical encoding: low 4 bits of last character must be 0",
                         data_len - 1)
    if rem == 3 and vals and vals[-1] & 0x03:
        raise CodecError("non-canonical encoding: low 2 bits of last character must be 0",
                         data_len - 1)

    out = bytearray()
    extend = out.extend
    i = 0
    stop = data_len - rem
    while i < stop:
        x = (vals[i] << 18) | (vals[i + 1] << 12) | (vals[i + 2] << 6) | vals[i + 3]
        extend(((x >> 16) & 0xFF, (x >> 8) & 0xFF, x & 0xFF))
        i += 4
    if rem == 2:
        out.append(((vals[i] << 6) | vals[i + 1]) >> 4)
    elif rem == 3:
        x = (vals[i] << 12) | (vals[i + 1] << 6) | vals[i + 2]
        extend(((x >> 10) & 0xFF, (x >> 2) & 0xFF))
    return bytes(out)
