"""Legacy v1 codec -- the defective implementation from production.

Frame layout (v1)::

    magic  = b"LC1"            # 3 bytes
    length = uint16 big-endian  # original/uncompressed length, max 65535
    body   = LZSS token stream

Tokens (a tag byte then payload bytes):

* literal byte b  -> tag 0x00, followed by b                    (2 bytes)
* match (off, ln) -> tag 0b1_llll_o  where llll = ln - 3 and o
                     is bit 8 of off, followed by off & 0xFF.
                     off in 1..512, ln in 3..18.                (2 bytes)

This module deliberately preserves the five production defects.  Do not use it
for new data; it exists purely for reproduction tests and reading history.
"""

MAGIC = b"LC1"
WINDOW = 512
MIN_MATCH = 3
MAX_MATCH = 18


def _salt() -> int:
    # PYTHONHASHSEED rotates this between processes; the greedy matcher used it
    # as a "random enough" tie breaker.  This is defect #3 (nondeterminism).
    return hash("lzss-seed") & 0xFFFF


def _find_match(data: bytes, pos: int):
    """Greedy window search used by the v1 encoder.

    Returns (distance, length); length is 0 when no match exists.
    """
    best_len = 0
    candidates = []
    if pos + MIN_MATCH > len(data):
        return 0, 0
    cand = data[pos:pos + MIN_MATCH]
    probe = max(0, pos - WINDOW)
    while probe < pos:
        if data[probe:probe + MIN_MATCH] == cand:
            ln = MIN_MATCH
            while (ln < MAX_MATCH
                   and pos + ln < len(data)
                   and data[probe + ln] == data[pos + ln]):
                ln += 1
            if ln > best_len:
                best_len = ln
                candidates = [probe]
            elif ln == best_len:
                candidates.append(probe)
        probe += 1
    if best_len >= MIN_MATCH:
        # Defect #3: equal-length matches are picked with a hash-salted
        # pseudo-random index, so output depends on the process hash seed.
        idx = (_salt() + pos) % len(candidates)
        return pos - candidates[idx], best_len
    return 0, 0


def compress(data: bytes) -> bytes:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes")
    if len(data) > 0xFFFF:
        raise ValueError("v1 supports at most 65535 bytes")
    out = bytearray(MAGIC)
    out += len(data).to_bytes(2, "big")
    pos = 0
    n = len(data)
    while pos < n:
        off, ln = _find_match(data, pos)
        if ln >= MIN_MATCH:
            # Defect #2: when the match runs to the very end of the input the
            # encoder emits one byte too few and stops, believing the stream
            # is complete.  Data ending on an end-of-input match therefore
            # loses its final byte after a round trip.
            if pos + ln == n:
                ln -= 1
                out.append(0x80 | ((ln - MIN_MATCH) << 1) | ((off >> 8) & 1))
                out.append(off & 0xFF)
                break
            out.append(0x80 | ((ln - MIN_MATCH) << 1) | ((off >> 8) & 1))
            out.append(off & 0xFF)
            pos += ln
        else:
            out.append(0x00)
            out.append(data[pos])
            pos += 1
    # Defect #1: there is no "stored" mode and the LZSS frame is always
    # returned, even when it is larger than the original data.
    return bytes(out)


def decompress(blob: bytes) -> bytes:
    # Defect #5: every failure collapses to the same generic message, so
    # callers cannot tell truncation apart from corruption or a bad header.
    try:
        if blob[:3] != MAGIC:
            raise RuntimeError("bad data")
        n = int.from_bytes(blob[3:5], "big")
        body = blob[5:]
        out = bytearray()
        i = 0
        # Defect #4: the declared original length n is only used to slice the
        # result; the decoder keeps no "expected token bytes" notion, so a
        # truncated token stream yields partial output without raising.
        while i < len(body):
            tag = body[i]
            i += 1
            if tag & 0x80:
                lo = body[i]
                i += 1
                off = ((tag & 1) << 8) | lo
                ln = ((tag >> 1) & 0x0F) + MIN_MATCH
                src = len(out) - off
                for _ in range(ln):
                    out.append(out[src])
                    src += 1
            else:
                out.append(body[i])
                i += 1
        return bytes(out[:n])
    except Exception:
        raise RuntimeError("bad data")


def inspect(blob: bytes):
    """Structural read helper for historical data -- does not hide defects.

    Returns ``(status, data)`` where status is one of:

    * ``("ok", data)``          -- the token stream exactly matches the
      declared length and appears recoverable;
    * ``("truncated", data)``   -- token stream ends early (defect #4);
      *data* is the recoverable prefix and must not be treated as complete;
    * ``("unrecoverable", data)`` -- framing is unreadable or tokens are
      inconsistent; data may be empty.

    It does not invent bytes for frames produced by the defect #2 encoder:
    those arrive as ``truncated`` because one token byte is genuinely gone.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return "unrecoverable", b""
    blob = bytes(blob)
    if len(blob) < 5 or blob[:3] != MAGIC:
        return "unrecoverable", b""
    declared = int.from_bytes(blob[3:5], "big")
    body = blob[5:]
    out = bytearray()
    i = 0
    try:
        while i < len(body):
            tag = body[i]
            i += 1
            if tag & 0x80:
                if i >= len(body):
                    return "truncated", bytes(out)
                lo = body[i]
                i += 1
                off = ((tag & 1) << 8) | lo
                ln = ((tag >> 1) & 0x0F) + MIN_MATCH
                src = len(out) - off
                if off < 1 or src < 0:
                    return "unrecoverable", bytes(out)
                for _ in range(ln):
                    if src >= len(out):
                        return "unrecoverable", bytes(out)
                    out.append(out[src])
                    src += 1
            else:
                if i >= len(body):
                    return "truncated", bytes(out)
                out.append(body[i])
                i += 1
    except Exception:
        return "unrecoverable", bytes(out)
    if len(out) < declared:
        return "truncated", bytes(out)
    return "ok", bytes(out[:declared])
