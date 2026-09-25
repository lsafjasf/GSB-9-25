"""Legacy compression implementation ("MC0" format).

This module intentionally preserves the pre-fix production code.  It exhibits
the five known classes of defects and is kept around for two reasons:

1. ``test_repro_legacy.py`` reproduces every defect against this code.
2. The migration notes in FORMAT.md describe how to recognise this format.

Frame layout (MC0), big-endian::

    magic      2 bytes  b"MC"
    orig_len   2 bytes  uint16  (never validated on decode)
    tokens...

Tokens::

    0x00              end of stream
    0b01nnnnn         literal run, n = 1..31 bytes follow
    0b1ooooooo        match: offset = 1..127, length = next byte + 3

Known defects (do not "fix" them here; fix the new implementation instead):

- Defect 1: no raw fallback, so incompressible small data expands.
- Defect 2: the last input byte that is not covered by a back-reference is
  silently dropped on encode.
- Defect 3: the module-level ``_rotation`` counter is never reset between
  calls and picks among equally-long matches, so compressing the same data
  twice in one process can emit different tokens.
- Defect 4: ``decompress`` silently returns whatever it managed to decode
  when the input is cut short.
- Defect 5: every failure raises a plain ``Exception`` with an unstructured
  message ("bad data") -- callers cannot distinguish causes, and there is no
  checksum of any kind.
"""

_MAGIC = b"MC"

# Module-level state that leaks between calls (Defect 3).  The rotation
# counter is advanced on every compress() and never reseeded, so successive
# compressions of the same input in one process can pick different offsets
# among equally-long matches.
_rotation = 0


def reset_state():
    """Clear the leaked encoder state (used by tests)."""
    global _rotation
    _rotation = 0


def compress(data: bytes) -> bytes:
    global _rotation
    out = bytearray()
    out += _MAGIC
    out += len(data).to_bytes(2, "big")

    seen = {}
    i = 0
    # Defect 2: the loop stops one byte early.  When the final byte is not
    # part of a back-reference (or the input has no match at all), it is
    # never emitted.
    while i < len(data) - 1:
        best_len = 0
        tied = []
        pat = data[i:i + 3]
        for p in seen.get(pat, ()):
            off = i - p
            if off > 127:
                continue
            n = 3
            limit = min(len(data) - 1 - i, 31)
            while n <= limit and data[p + n] == data[i + n]:
                n += 1
            n -= 1
            if n >= 3:
                if n > best_len:
                    best_len = n
                    tied = [off]
                elif n == best_len:
                    tied.append(off)
        if tied:
            # Leaked state decides tie-breaks -> non-deterministic across
            # successive calls within the same process.
            best_off = tied[_rotation % len(tied)]
        if best_len >= 3:
            off = best_off
            out.append(0x80 | off)
            out.append(best_len - 3)
            for k in range(i, i + best_len):
                seen.setdefault(data[k:k + 3], []).append(k)
            i += best_len
        else:
            j = i
            while j < len(data) - 1 and j - i < 31:
                if j + 2 < len(data) and data[j:j + 3] in seen:
                    cand = seen[data[j:j + 3]]
                    if any(j - p <= 127 for p in cand):
                        break
                j += 1
            n = j - i
            if n > 0:
                out.append(0x40 | n)
                out += data[i:j]
            for k in range(i, j):
                seen.setdefault(data[k:k + 3], []).append(k)
            i = j if n > 0 else i + 1
    out.append(0x00)
    _rotation += 1
    return bytes(out)


def decompress(buf: bytes) -> bytes:
    # Defect 5: one undifferentiated exception type/message for everything.
    try:
        if buf[0:2] != _MAGIC:
            raise Exception("bad data")
        i = 4
        out = bytearray()
        while i < len(buf):
            tag = buf[i]
            i += 1
            if tag == 0x00:
                # Defect 4: end token (or simply running out of bytes) ends
                # decoding and whatever was produced is returned as success.
                return bytes(out)
            elif tag & 0x40 and not tag & 0x80:
                n = tag & 0x1F
                out += buf[i:i + n]  # missing bytes are silently absent
                i += n
            elif tag & 0x80:
                off = tag & 0x7F
                length = buf[i] + 3
                i += 1
                start = len(out) - off
                for _ in range(length):
                    out.append(out[start])
                    start += 1
            else:
                raise Exception("bad data")
        # Defect 4 again: truncated input with no end token still "succeeds".
        return bytes(out)
    except Exception as exc:
        if exc.args == ("bad data",):
            raise
        raise Exception("bad data")
