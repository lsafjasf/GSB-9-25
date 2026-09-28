"""Fixed variable-length small-block compression codec.

Format: MCP1 (see FORMAT.md for the full specification and the comparison
with the legacy MC0 format).

Frame layout, all integers big-endian::

    offset  size  field
    0       4     magic           b"MCP1"
    4       1     mode            0 = RAW (stored), 1 = LZ
    5       4     orig_len        uint32, length of the *decoded* payload
    9       4     crc32           zlib.crc32 of the *decoded* payload
    13      ...   payload

RAW payload is the original bytes verbatim.

LZ payload tokens::

    0x00                  end of stream
    0x01..0x3F            literal run: next (tag) bytes are copied literally
    0x80..0x8F + 4 bytes  match: offset = (tag & 0x0F) << 24 | u24be,
                          length = 3 + next byte  (1 <= offset <= 2^28-1)
    0x40..0x7F, 0x90..0xFF reserved

Compression is deterministic: identical input always yields identical bytes.
RAW mode is selected whenever the compressed frame would not be strictly
smaller than the stored frame -- incompressible data is never expanded.

Decompression never returns partial output: a frame is decoded fully into a
local buffer and only returned after length, structural and CRC checks all
pass.  Failures use distinct exception classes:

    HeaderError    -- illegal magic / mode / frame grammar / trailing bytes
    TruncatedError -- frame or token ends before its declared data
    ChecksumError  -- structurally complete frame with a CRC mismatch
"""

import zlib
from array import array

MAGIC = b"MCP1"
HEADER_SIZE = 13

MODE_RAW = 0
MODE_LZ = 1

MAX_LITERAL = 0x3F           # 63
MAX_OFFSET = 0x0FFFFFFF      # 28-bit back-reference window (~256 MiB)
MAX_MATCH_ADD = 0xFF         # match length 3..258
MAX_DECODED_SIZE = 64 * 1024 * 1024


class Error(Exception):
    """Base class for all codec errors."""


class HeaderError(Error):
    """Illegal frame envelope or token grammar (magic, mode, reserved tags)."""


class TruncatedError(Error):
    """The frame ends before all declared bytes/token data are present."""


class ChecksumError(Error):
    """Structurally complete frame whose payload fails the CRC-32 check."""


HASH_BITS = 20
HASH_SIZE = 1 << HASH_BITS
HASH_MASK = HASH_SIZE - 1

# A plain backward scan is the cheapest possible search while it decides
# quickly (a max-length match within the nearest few candidates, or very
# few candidates at all).  Only when one query examines more than this
# many candidates without reaching a decision does the encoder build the
# 3-byte hash-chain index and use it for the rest of the stream.
_SCAN_CAP = 256


def _lz_encode(data: bytes) -> bytearray:
    """Deterministic LZSS-style encoder.

    Ties between equal-length matches always resolve to the nearest offset,
    and no call-global state is consulted, so output depends only on input.

    Search strategy: each position first tries a nearest-first backward
    scan capped at ``_SCAN_CAP`` candidates.  The scan decides exactly when
    it finds a max-length match or runs out of candidates; if it would
    scan further, the encoder switches (once, permanently) to a hash-chain
    index over 3-byte prefixes.  Two different 3-byte prefixes can never
    form a length-3 match, so chain candidates are exactly the scan
    candidates worth measuring, still visited nearest-first with strict
    '>' updates: the emitted tokens are byte-identical to a full backward
    scan, while low-match-density data (the kind that ends up stored RAW)
    costs expected-linear instead of quadratic time.  The hash mixes fixed
    constants only -- no dependence on the process hash seed.
    """
    payload = bytearray()
    n = len(data)
    head = None          # hash-chain index, built lazily on first hard query
    prev = None
    indexed_upto = 0     # every position < indexed_upto is in the chain
    i = 0
    while i < n:
        best_len = 0
        best_off = 0
        if i + 3 <= n:
            limit = min(n - i, MAX_MATCH_ADD + 3)
            if head is None:
                # Cheap path: nearest-first scan, capped.  Strict '>'
                # keeps the nearest offset on ties.
                p = i - 1
                scanned = 0
                decided = False
                while p >= 0:
                    off = i - p
                    if off > MAX_OFFSET:
                        decided = True
                        break
                    ml = 0
                    while ml < limit and data[p + ml] == data[i + ml]:
                        ml += 1
                    if ml > best_len:
                        best_len = ml
                        best_off = off
                        if ml == limit:
                            decided = True
                            break
                    scanned += 1
                    if scanned >= _SCAN_CAP:
                        break
                    p -= 1
                else:
                    decided = True      # ran out of candidates
                if not decided:
                    head = array("i", [-1]) * HASH_SIZE
                    prev = array("i", [-1]) * n
            if head is not None:
                # Index every position passed since the previous query,
                # then walk this position's chain nearest-first.
                k = indexed_upto
                while k < i:
                    if k + 3 <= n:
                        h = ((data[k] * 54059) ^ (data[k + 1] * 7699)
                             ^ (data[k + 2] * 8697)) & HASH_MASK
                        prev[k] = head[h]
                        head[h] = k
                    k += 1
                indexed_upto = i
                h = ((data[i] * 54059) ^ (data[i + 1] * 7699)
                     ^ (data[i + 2] * 8697)) & HASH_MASK
                p = head[h]
                while p >= 0:
                    off = i - p
                    if off > MAX_OFFSET:
                        break
                    ml = 0
                    while ml < limit and data[p + ml] == data[i + ml]:
                        ml += 1
                    if ml > best_len:
                        best_len = ml
                        best_off = off
                        if ml == limit:
                            break
                    p = prev[p]
        if best_len >= 3:
            tag_byte = 0x80 | ((best_off >> 24) & 0x0F)
            payload.append(tag_byte)
            payload.append((best_off >> 16) & 0xFF)
            payload.append((best_off >> 8) & 0xFF)
            payload.append(best_off & 0xFF)
            payload.append(best_len - 3)
            i += best_len
        else:
            j = min(i + MAX_LITERAL, n)
            payload.append(j - i)
            payload += data[i:j]
            i = j
    payload.append(0x00)
    return payload


def compress(data: bytes) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes-like")
    data = bytes(data)
    # Same ceiling the decoder enforces: a frame that could never be
    # decompressed must not be produced in the first place.
    if len(data) > MAX_DECODED_SIZE:
        raise HeaderError(
            "input length %d exceeds MCP1 decoded-size limit of %d bytes"
            % (len(data), MAX_DECODED_SIZE)
        )

    lz_payload = bytes(_lz_encode(data))
    stored_size = len(data)
    # Choose LZ only when the frame is strictly smaller.  Ties go to RAW:
    # stored data is cheaper to decode and carries no parsing ambiguity.
    if len(lz_payload) < stored_size:
        mode = MODE_LZ
        payload = lz_payload
    else:
        mode = MODE_RAW
        payload = data

    frame = bytearray()
    frame += MAGIC
    frame.append(mode)
    frame += len(data).to_bytes(4, "big")
    frame += zlib.crc32(data).to_bytes(4, "big")
    frame += payload
    return bytes(frame)


def _decompress_raw(mode_payload: bytes, orig_len: int, crc: int) -> bytes:
    if len(mode_payload) != orig_len:
        raise TruncatedError(
            "RAW payload length mismatch: header declares %d, frame has %d"
            % (orig_len, len(mode_payload))
        )
    data = mode_payload
    if zlib.crc32(data) & 0xFFFFFFFF != crc:
        raise ChecksumError("CRC-32 mismatch in RAW frame")
    return bytes(data)


def _decompress_lz(mode_payload: bytes, orig_len: int, crc: int) -> bytes:
    out = bytearray()
    length = len(mode_payload)
    i = 0
    while True:
        if i >= length:
            raise TruncatedError("LZ stream ends without an end-of-stream tag")
        tag = mode_payload[i]
        i += 1
        if tag == 0x00:
            break
        if 0x01 <= tag <= MAX_LITERAL:
            if i + tag > length:
                raise TruncatedError(
                    "literal run declares %d bytes, %d remain"
                    % (tag, length - i)
                )
            out += mode_payload[i:i + tag]
            i += tag
        elif 0x80 <= tag <= 0x8F:
            if i + 4 > length:
                raise TruncatedError("match token truncated")
            offset = (((tag & 0x0F) << 24)
                      | (mode_payload[i] << 16)
                      | (mode_payload[i + 1] << 8)
                      | mode_payload[i + 2])
            match_len = mode_payload[i + 3] + 3
            i += 4
            if offset == 0:
                raise HeaderError("match offset 0 is illegal")
            if offset > len(out):
                raise HeaderError(
                    "match offset %d points before start of stream" % offset
                )
            if len(out) + match_len > orig_len:
                raise HeaderError(
                    "match extends beyond declared length %d" % orig_len
                )
            src = len(out) - offset
            for _ in range(match_len):
                out.append(out[src])
                src += 1
        else:
            # 0x40..0x7F and 0x90..0xFF are reserved/illegal.
            raise HeaderError("reserved LZ token 0x%02X at offset %d"
                              % (tag, i - 1))

    if len(out) != orig_len:
        raise TruncatedError(
            "decoded length %d does not match header length %d"
            % (len(out), orig_len)
        )
    if i != length:
        raise HeaderError("%d trailing bytes after end-of-stream tag"
                          % (length - i))
    if zlib.crc32(out) & 0xFFFFFFFF != crc:
        raise ChecksumError("CRC-32 mismatch in LZ frame")
    return bytes(out)


def decompress(frame: bytes) -> bytes:
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise TypeError("frame must be bytes-like")
    frame = bytes(frame)

    if len(frame) < HEADER_SIZE:
        raise TruncatedError(
            "frame shorter than %d-byte header (%d bytes)"
            % (HEADER_SIZE, len(frame))
        )
    if frame[0:4] != MAGIC:
        raise HeaderError("bad magic: not an MCP1 frame")
    mode = frame[4]
    if mode not in (MODE_RAW, MODE_LZ):
        raise HeaderError("unknown storage mode 0x%02X" % mode)

    orig_len = int.from_bytes(frame[5:9], "big")
    crc = int.from_bytes(frame[9:13], "big")
    if orig_len > MAX_DECODED_SIZE:
        raise HeaderError("declared length exceeds safety limit")

    mode_payload = frame[HEADER_SIZE:]
    if mode == MODE_RAW:
        data = _decompress_raw(mode_payload, orig_len, crc)
    else:
        data = _decompress_lz(mode_payload, orig_len, crc)

    return data
