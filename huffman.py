"""Deterministic Huffman compression library (Python standard library only).

Archive format (all fixed-width integers are big-endian)::

    offset  size  content
    0       4     magic b"HUF1"
    4       8     original length in bytes (uint64)
    12      2     number of table entries N (uint16)
    14      2N    table entries (symbol uint8, code length uint8), sorted by symbol
    ...     1     number of padding bits in the last payload byte (0..7)
    ...     16    first 16 bytes of SHA-256 of the original data
    ...     rest  bitstream: canonical Huffman codes, packed MSB first

Determinism: the heap tie-breaks on (frequency, smallest symbol in subtree),
and codes are assigned canonically (sorted by length, then symbol), so the
same input always produces the exact same output bytes.
"""

from __future__ import annotations

import hashlib
import heapq
import struct

MAGIC = b"HUF1"
MAX_CODE_LENGTH = 255
_DIGEST_SIZE = 16
_HEADER_SIZE = 14  # magic + uint64 length + uint16 count


class HuffmanError(Exception):
    """Base class for all errors raised by this library."""


class FormatError(HuffmanError):
    """Bad magic bytes or otherwise malformed header fields."""


class TruncatedError(HuffmanError):
    """The archive ends before the header, table, or bitstream is complete."""


class InvalidCodeTableError(HuffmanError):
    """The stored code table is not a valid complete prefix code."""


class TrailingDataError(HuffmanError):
    """Extra bytes/bits or non-zero padding after the final symbol."""


class BitstreamMismatchError(HuffmanError):
    """The bitstream contains a code that does not exist in the table."""


class IntegrityError(HuffmanError):
    """Decoded data does not match the stored SHA-256 digest."""


def _build_code_lengths(freqs: dict[int, int]) -> dict[int, int]:
    """Build Huffman code lengths with a fully deterministic tie-break."""
    if len(freqs) == 1:
        # Degenerate tree: give the single symbol a 1-bit code.
        return {next(iter(freqs)): 1}
    heap: list[tuple[int, int, int, object]] = []
    seq = 0
    for sym in sorted(freqs):
        heap.append((freqs[sym], sym, seq, sym))
        seq += 1
    heapq.heapify(heap)
    while len(heap) > 1:
        f1, m1, _, n1 = heapq.heappop(heap)
        f2, m2, _, n2 = heapq.heappop(heap)
        heapq.heappush(heap, (f1 + f2, min(m1, m2), seq, (n1, n2)))
        seq += 1
    root = heap[0][3]
    lengths: dict[int, int] = {}
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, int):
            lengths[node] = depth
        else:
            left, right = node
            stack.append((left, depth + 1))
            stack.append((right, depth + 1))
    return lengths


def _canonical_codes(lengths: dict[int, int]) -> dict[int, tuple[int, int]]:
    """Assign canonical codes: sort by (length, symbol), count up."""
    codes: dict[int, tuple[int, int]] = {}
    code = 0
    prev_len = 0
    for sym, length in sorted(lengths.items(), key=lambda kv: (kv[1], kv[0])):
        code <<= length - prev_len
        codes[sym] = (code, length)
        code += 1
        prev_len = length
    return codes


def _pack_bits(data: bytes, codes: dict[int, tuple[int, int]]) -> tuple[bytes, int]:
    out = bytearray()
    append = out.append
    acc = 0
    acc_bits = 0
    for b in data:
        code, length = codes[b]
        acc = (acc << length) | code
        acc_bits += length
        while acc_bits >= 8:
            acc_bits -= 8
            append((acc >> acc_bits) & 0xFF)
            acc &= (1 << acc_bits) - 1
    padding = (-acc_bits) % 8
    if padding:
        append((acc << padding) & 0xFF)
    return bytes(out), padding


def compress(data: bytes) -> bytes:
    """Compress ``data`` into a deterministic self-contained archive."""
    data = bytes(data)
    freqs: dict[int, int] = {}
    for b in data:
        freqs[b] = freqs.get(b, 0) + 1
    if freqs:
        lengths = _build_code_lengths(freqs)
        codes = _canonical_codes(lengths)
        table = sorted(lengths.items())  # sorted by symbol: fully deterministic
        payload, padding = _pack_bits(data, codes)
    else:
        table = []
        payload, padding = b"", 0
    header = bytearray()
    header += MAGIC
    header += struct.pack(">QH", len(data), len(table))
    for sym, length in table:
        header += bytes((sym, length))
    header.append(padding)
    header += hashlib.sha256(data).digest()[:_DIGEST_SIZE]
    return bytes(header) + payload


def _parse_table(raw: bytes, count: int) -> dict[int, int]:
    lengths: dict[int, int] = {}
    for i in range(count):
        sym = raw[2 * i]
        length = raw[2 * i + 1]
        if length == 0:
            raise InvalidCodeTableError("code length of zero in table")
        if sym in lengths:
            raise InvalidCodeTableError(f"duplicate symbol {sym:#04x} in table")
        lengths[sym] = length
    if count == 0:
        return lengths
    if count == 1:
        if next(iter(lengths.values())) != 1:
            raise InvalidCodeTableError("single-symbol table must use length 1")
        return lengths
    max_len = max(lengths.values())
    # Kraft equality: the lengths must describe a complete prefix code.
    kraft = sum(1 << (max_len - length) for length in lengths.values())
    if kraft != 1 << max_len:
        raise InvalidCodeTableError("code lengths are not a complete prefix code")
    return lengths


class _BitReader:
    __slots__ = ("data", "nbytes", "byte_pos", "buf", "buf_bits")

    def __init__(self, data: bytes):
        self.data = data
        self.nbytes = len(data)
        self.byte_pos = 0
        self.buf = 0
        self.buf_bits = 0

    @property
    def remaining(self) -> int:
        return (self.nbytes - self.byte_pos) * 8 + self.buf_bits

    def _fill(self, need: int) -> None:
        while self.buf_bits < need and self.byte_pos < self.nbytes:
            chunk = self.data[self.byte_pos:self.byte_pos + 8]
            self.buf = (self.buf << (8 * len(chunk))) | int.from_bytes(chunk, "big")
            self.buf_bits += 8 * len(chunk)
            self.byte_pos += len(chunk)

    def peek(self, n: int) -> int:
        """Return the next ``n`` bits; caller guarantees availability."""
        if self.buf_bits < n:
            self._fill(n)
        return (self.buf >> (self.buf_bits - n)) & ((1 << n) - 1)

    def drop(self, n: int) -> None:
        self.buf_bits -= n
        self.buf &= (1 << self.buf_bits) - 1


def _decode_payload(payload: bytes, orig_len: int, lengths: dict[int, int],
                    padding: int) -> bytes:
    max_len = max(lengths.values())
    codes = _canonical_codes(lengths)
    table_bits = min(max_len, 12)
    lookup: list[tuple[int, int] | None] = [None] * (1 << table_bits)
    long_codes: dict[tuple[int, int], int] = {}
    for sym, (code, length) in codes.items():
        if length <= table_bits:
            lo = code << (table_bits - length)
            for slot in range(lo, lo + (1 << (table_bits - length))):
                lookup[slot] = (sym, length)
        else:
            long_codes[(length, code)] = sym
    reader = _BitReader(payload)
    out = bytearray()
    append = out.append
    for _ in range(orig_len):
        avail = reader.remaining
        if avail == 0:
            raise TruncatedError("bitstream ended before all symbols were decoded")
        n = table_bits if avail >= table_bits else avail
        window = reader.peek(n)
        if n < table_bits:
            window <<= table_bits - n
        entry = lookup[window]
        if entry is not None:
            sym, length = entry
            if length > avail:
                raise TruncatedError("bitstream ended mid-code")
            append(sym)
            reader.drop(length)
            continue
        # Rare path: code longer than the lookup table.
        for length in range(table_bits + 1, max_len + 1):
            if reader.remaining < length:
                raise TruncatedError("bitstream ended mid-code")
            sym = long_codes.get((length, reader.peek(length)))
            if sym is not None:
                append(sym)
                reader.drop(length)
                break
        else:
            raise BitstreamMismatchError("bit pattern matches no code in table")
    remaining = reader.remaining
    if remaining < padding:
        raise TruncatedError("declared padding exceeds remaining bits")
    if remaining > padding:
        raise TrailingDataError(
            f"{remaining - padding} extra bit(s) after the final symbol")
    if padding and reader.peek(padding) != 0:
        raise TrailingDataError("non-zero padding bits at end of stream")
    return bytes(out)


def decompress(blob: bytes) -> bytes:
    """Decompress an archive produced by :func:`compress`.

    Raises a subclass of :class:`HuffmanError` on any corruption; never
    returns partial output.
    """
    blob = bytes(blob)
    if len(blob) < 4:
        raise TruncatedError("input shorter than magic header")
    if blob[:4] != MAGIC:
        raise FormatError("bad magic bytes")
    if len(blob) < _HEADER_SIZE:
        raise TruncatedError("truncated header")
    orig_len, count = struct.unpack(">QH", blob[4:_HEADER_SIZE])
    table_end = _HEADER_SIZE + 2 * count
    if len(blob) < table_end + 1 + _DIGEST_SIZE:
        raise TruncatedError("truncated code table or header")
    lengths = _parse_table(blob[_HEADER_SIZE:table_end], count)
    padding = blob[table_end]
    if padding > 7:
        raise FormatError(f"invalid padding bit count {padding}")
    digest = blob[table_end + 1:table_end + 1 + _DIGEST_SIZE]
    payload = blob[table_end + 1 + _DIGEST_SIZE:]
    if count == 0:
        if orig_len != 0:
            raise BitstreamMismatchError("empty code table but non-zero length")
        if padding != 0 or payload:
            raise TrailingDataError("unexpected data after empty stream")
        data = b""
    else:
        data = _decode_payload(payload, orig_len, lengths, padding)
    if hashlib.sha256(data).digest()[:_DIGEST_SIZE] != digest:
        raise IntegrityError("decoded data fails the integrity check")
    return data
