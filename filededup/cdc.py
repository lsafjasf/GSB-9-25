"""Content-defined chunking (CDC) based on a gear rolling hash (FastCDC-style).

A 64-bit fingerprint `fp` is rolled over the byte stream:

    fp = (fp << 1) ^ GEAR[byte]        (mod 2**64)

A byte influences `fp` for the next 64 steps, so `fp` is a hash of the
last ~64 bytes seen.  A chunk boundary is declared at position p when

    fp & mask == 0

where mask has ~log2(avg) bits set, giving an expected chunk length near
`avg_size`.  Because the condition depends only on the *content* of the
sliding window (plus the min/max guards), inserting or deleting bytes
elsewhere does not move later boundaries: after an edit the chunker
re-synchronizes within one or two chunks.

Normalization (as in FastCDC): a stricter mask is used before the
average size and a looser one after it, which tightens the chunk-size
distribution around `avg_size`.

Everything is streamed: the chunker reads `read_size` bytes at a time
and keeps at most one chunk (< `max_size`) plus the read buffer in
memory, independent of the total file size.
"""

from __future__ import annotations

import random
from typing import BinaryIO, Iterator

_MASK64 = (1 << 64) - 1


def _build_gear() -> list[int]:
    rng = random.Random(0x9E3779B97F4A7C15)
    return [rng.getrandbits(64) for _ in range(256)]


GEAR: list[int] = _build_gear()

DEFAULT_MIN_SIZE = 4 * 1024
DEFAULT_AVG_SIZE = 16 * 1024
DEFAULT_MAX_SIZE = 64 * 1024
READ_SIZE = 1 << 20


class Chunker:
    """Streaming content-defined chunker. Reusable across files."""

    def __init__(
        self,
        min_size: int = DEFAULT_MIN_SIZE,
        avg_size: int = DEFAULT_AVG_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
    ) -> None:
        if not (0 < min_size <= avg_size <= max_size):
            raise ValueError("require 0 < min_size <= avg_size <= max_size")
        self.min_size = min_size
        self.avg_size = avg_size
        self.max_size = max_size
        bits = avg_size.bit_length() - 1
        self._mask_s = (1 << (bits + 1)) - 1  # strict, before avg_size
        self._mask_l = (1 << (bits - 1)) - 1  # loose, after avg_size

    def iter_chunks(
        self, stream: BinaryIO, read_size: int = READ_SIZE
    ) -> Iterator[tuple[int, bytes]]:
        """Yield (offset, chunk_bytes) for each content-defined chunk.

        Only `stream.read(n)` is used, so any file-like object works and
        memory stays bounded by read_size + max_size.
        """
        min_size = self.min_size
        avg_size = self.avg_size
        max_size = self.max_size
        mask_s = self._mask_s
        mask_l = self._mask_l
        gear = GEAR
        mask64 = _MASK64

        fp = 0
        pending = bytearray()
        offset = 0
        while True:
            data = stream.read(read_size)
            if not data:
                break
            i, n = 0, len(data)
            while i < n:
                need = min_size - len(pending)
                if need > 0:
                    take = need if need < n - i else n - i
                    pending += data[i : i + take]
                    i += take
                    if len(pending) < min_size:
                        continue
                    fp = 0
                    for b in pending[-64:]:
                        fp = ((fp << 1) ^ gear[b]) & mask64
                room = max_size - len(pending)
                limit = i + room
                if limit > n:
                    limit = n
                base = len(pending)
                cut = -1
                j = i
                while j < limit:
                    fp = ((fp << 1) ^ gear[data[j]]) & mask64
                    cur = base + (j - i) + 1
                    if fp & (mask_s if cur < avg_size else mask_l) == 0:
                        cut = j
                        break
                    j += 1
                if cut >= 0:
                    end = cut + 1
                    pending += data[i:end]
                    yield offset, bytes(pending)
                    offset += len(pending)
                    pending.clear()
                    fp = 0
                    i = end
                else:
                    pending += data[i:limit]
                    i = limit
                    if len(pending) >= max_size:
                        yield offset, bytes(pending)
                        offset += len(pending)
                        pending.clear()
                        fp = 0
        if pending:
            yield offset, bytes(pending)
