"""Fixed v2 codec for variable-length small blocks.

Goals (each maps to a fixed production defect):

* never expands the payload beyond a constant 17-byte frame overhead -- if
  DEFLATE does not pay off the block is kept verbatim under an explicit
  STORED mode the reader recognises;
* lossless round trip for every input, with the original length carried in
  the frame and checked after decoding;
* byte-for-byte deterministic output (fixed algorithm settings, no hashing
  or timing-dependent choices);
* truncated frames, checksum failures and illegal headers raise distinct
  errors and never return partial output.

Frame layout (all integers big-endian)::

    offset  size  field
    0       3     magic = b"MC2"
    3       1     version = 0x02
    4       1     flags  (bit 0 = mode, bits 1..7 reserved, must be zero)
    5       4     original_len   (uncompressed length)
    9       4     payload_len    (exact number of payload bytes)
    13      4     crc32          (CRC-32/IEEE of the payload)
    17      ...   payload

Modes:

* STORED  (flags bit 0 = 0): payload is the raw input.
* DEFLATE (flags bit 0 = 1): payload is a raw DEFLATE stream (RFC 1951,
  zlib wbits=-15), selected only when it is strictly shorter than raw.
"""

import zlib

MAGIC = b"MC2"
VERSION = 0x02
HEADER_LEN = 17

MODE_STORED = 0
MODE_DEFLATE = 1
_FLAG_MODE = 0x01

# Deterministic, standard-library-only compression settings.
_LEVEL = 9
_WBITS = -15  # raw DEFLATE, no zlib wrapper

MAX_LEN = 0xFFFFFFFF


class CompressionError(Exception):
    """Base class for every v2 decoding failure."""


class TruncatedError(CompressionError):
    """The frame ends before the header/payload declared by its fields."""


class HeaderError(CompressionError):
    """The header is malformed or contradicts the frame body."""


class ChecksumError(CompressionError):
    """The payload CRC-32 does not match the value stored in the header."""


class CorruptPayloadError(ChecksumError):
    """CRC matches structurally but the payload cannot decode to the
    declared original length (broken DEFLATE stream / length mismatch).

    Subclasses :class:`ChecksumError` so callers can treat it as a payload
    integrity failure while still distinguishing it from a CRC mismatch.
    """


def _frame(flags: int, original: bytes, payload: bytes) -> bytes:
    if len(original) > MAX_LEN or len(payload) > MAX_LEN:
        raise ValueError("input too large for v2 frame")
    header = (
        MAGIC
        + bytes([VERSION, flags])
        + len(original).to_bytes(4, "big")
        + len(payload).to_bytes(4, "big")
        + zlib.crc32(payload).to_bytes(4, "big", signed=False)
    )
    return header + payload


def compress(data: bytes) -> bytes:
    """Return a deterministic v2 frame for *data*.

    STORED is used unless raw DEFLATE is *strictly* shorter, so the payload
    portion can never exceed the input size.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes-like")
    data = bytes(data)
    if len(data) > MAX_LEN:
        raise ValueError("input too large for v2 frame")

    if data:
        co = zlib.compressobj(_LEVEL, zlib.DEFLATED, _WBITS)
        deflated = co.compress(data) + co.flush()
    else:
        deflated = b""

    if deflated and len(deflated) < len(data):
        return _frame(_FLAG_MODE, data, deflated)
    return _frame(0, data, data)


def _header_error_if(condition: bool, message: str) -> None:
    if condition:
        raise HeaderError(message)


def decompress(blob: bytes) -> bytes:
    """Validate and decode a v2 frame.

    No data is returned until framing, CRC and payload decoding have all
    succeeded, so every failure path either raises or returns the complete
    output -- never partial output.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise TypeError("blob must be bytes-like")
    blob = bytes(blob)

    # 1) fixed header must be fully present.
    if len(blob) < HEADER_LEN:
        raise TruncatedError(
            "frame truncated: %d < %d header bytes" % (len(blob), HEADER_LEN))

    # 2) well-formed header fields.
    _header_error_if(blob[:3] != MAGIC, "bad magic: not a v2 frame")
    _header_error_if(blob[3] != VERSION, "unsupported frame version")
    flags = blob[4]
    _header_error_if(flags & ~_FLAG_MODE, "reserved flag bits are set")
    mode = flags & _FLAG_MODE
    _header_error_if(mode not in (MODE_STORED, MODE_DEFLATE),
                     "unknown storage mode")
    original_len = int.from_bytes(blob[5:9], "big")
    payload_len = int.from_bytes(blob[9:13], "big")
    crc_expected = int.from_bytes(blob[13:17], "big")

    # NOTE: in DEFLATE mode payload length is not cross-checked against the
    # original length here.  "deflate must be shorter" is a property the
    # encoder guarantees, not a structural framing requirement; a payload
    # that inflates badly or to the wrong length is reported later as
    # CorruptPayloadError.
    if mode == MODE_STORED:
        _header_error_if(
            payload_len != original_len,
            "stored frame payload length %d != original length %d"
            % (payload_len, original_len))

    # 3) exact payload boundary: missing bytes => truncation, extra bytes
    #    => a framing/header contradiction rather than silent acceptance.
    body = blob[HEADER_LEN:]
    if len(body) < payload_len:
        raise TruncatedError(
            "frame truncated: payload has %d of %d declared bytes"
            % (len(body), payload_len))
    _header_error_if(
        len(body) > payload_len,
        "frame contains %d trailing bytes after payload"
        % (len(body) - payload_len))
    payload = body

    # 4) integrity: CRC must match before any payload decoding is trusted.
    crc_actual = zlib.crc32(payload) & 0xFFFFFFFF
    if crc_actual != crc_expected:
        raise ChecksumError(
            "crc32 mismatch: header=%08x payload=%08x"
            % (crc_expected, crc_actual))

    # 5) mode-specific decode into a local; only return after full success.
    if mode == MODE_STORED:
        result = payload
    else:
        dec = zlib.decompressobj(_WBITS)
        try:
            result = dec.decompress(payload)
            result += dec.flush()
        except zlib.error as exc:
            raise CorruptPayloadError("deflate stream is corrupt: %s" % exc)
        if dec.unused_data:
            raise CorruptPayloadError("deflate stream contains trailing data")
        if len(result) != original_len:
            raise CorruptPayloadError(
                "decoded length %d != declared original length %d"
                % (len(result), original_len))
    return result
