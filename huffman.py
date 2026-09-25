"""Deterministic Huffman compression library (Python 3, stdlib only).

No compression modules are used. The only stdlib imports are `heapq`,
`struct` and `hashlib` (SHA-256 is used purely as an integrity checksum,
not for compression).

Container format (all integers big-endian)::

    offset  size  field
    0       4     magic b"HUF1"
    4       8     SHA-256[:8] of the original payload
    12      8     u64: original length in bytes (symbol count)
    20      2     u16: number of code-table entries K
    22      2*K   entries: (u8 symbol, u8 code length), sorted by symbol
    ...     ...   bitstream, final byte zero-padded

Codes are canonical: given the code lengths, the code for each symbol is
fully determined by sorting (length, symbol) pairs and counting up.  Code
lengths come from a Huffman tree whose merge order is fully deterministic
(ties broken by the smallest symbol contained in each subtree), so the
same input always produces byte-identical output.
"""

import hashlib
import heapq
import struct
from collections import Counter

MAGIC = b"HUF1"
_HEADER = struct.Struct(">4s8sQH")  # magic, checksum, orig_len, table_count
_MAX_CODE_LEN = 64  # sanity bound accepted by the decoder
_TABLE_WIDTH = 16   # fast decode table width in bits


class HuffmanError(Exception):
    """Base class for all decoding failures."""


class BadMagicError(HuffmanError):
    """Container does not start with the expected magic bytes."""


class TruncatedStreamError(HuffmanError):
    """The container or bitstream ends before the data is complete."""


class InvalidCodeTableError(HuffmanError):
    """The stored code table is malformed or not a valid prefix code."""


class TrailingBitsError(HuffmanError):
    """Extra non-padding bits/bytes follow the end of the bitstream."""


class DataMismatchError(HuffmanError):
    """Decoded data does not match the stored checksum (table/data mismatch)."""


# --------------------------------------------------------------------------
# Code construction (deterministic)
# --------------------------------------------------------------------------

def build_code_lengths(freqs):
    """Build deterministic Huffman code lengths from a {symbol: freq} map.

    Merge order is fully determined: the heap is ordered by
    (frequency, smallest symbol in subtree, insertion sequence), so any
    frequency tie is always resolved the same way.
    """
    if not freqs:
        return {}
    if len(freqs) == 1:
        # Degenerate single-symbol alphabet: one bit per symbol.
        return {next(iter(freqs)): 1}

    seq = 0
    heap = []
    for sym, freq in sorted(freqs.items()):
        heap.append((freq, sym, seq, (sym,)))
        seq += 1
    heapq.heapify(heap)

    while len(heap) > 1:
        f1, m1, _, t1 = heapq.heappop(heap)
        f2, m2, _, t2 = heapq.heappop(heap)
        heapq.heappush(heap, (f1 + f2, min(m1, m2), seq, (t1, t2)))
        seq += 1

    lengths = {}
    stack = [(heap[0][3], 0)]
    while stack:
        node, depth = stack.pop()
        if len(node) == 1:
            lengths[node[0]] = depth
        else:
            stack.append((node[0], depth + 1))
            stack.append((node[1], depth + 1))
    return lengths


def _validate_lengths(lengths):
    """Check that a {symbol: length} table is a usable prefix code."""
    if not lengths:
        raise InvalidCodeTableError("empty code table")
    if len(lengths) > 256:
        raise InvalidCodeTableError("too many symbols in code table")
    for sym, ln in lengths.items():
        if not (0 <= sym <= 255):
            raise InvalidCodeTableError("symbol out of byte range: %r" % (sym,))
        if not (1 <= ln <= _MAX_CODE_LEN):
            raise InvalidCodeTableError("invalid code length %d" % ln)
    if len(lengths) == 1:
        # Degenerate alphabet: we define the single code to be "0" (1 bit).
        if next(iter(lengths.values())) != 1:
            raise InvalidCodeTableError(
                "single-symbol table must use a 1-bit code")
        return
    # Kraft equality must hold exactly for a complete prefix code.
    maxlen = max(lengths.values())
    kraft = sum(1 << (maxlen - ln) for ln in lengths.values())
    if kraft != (1 << maxlen):
        raise InvalidCodeTableError(
            "code lengths do not form a complete prefix code")


def canonical_codes(lengths):
    """Assign canonical prefix codes for the given lengths.

    Symbols are sorted by (length, symbol); codes count up from zero.
    Deterministic for a fixed length table.
    """
    _validate_lengths(lengths)
    codes = {}
    code = 0
    prev_len = None
    for ln, sym in sorted((ln, sym) for sym, ln in lengths.items()):
        if prev_len is None:
            prev_len = ln
        else:
            code <<= (ln - prev_len)
            prev_len = ln
        if code >= (1 << ln):
            raise InvalidCodeTableError("oversubscribed code lengths")
        codes[sym] = (code, ln)
        code += 1
    return codes


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

def encode(data):
    """Compress `data` (bytes-like) into a deterministic container."""
    data = bytes(data)
    lengths = build_code_lengths(Counter(data))
    codes = canonical_codes(lengths) if lengths else {}

    out = bytearray()
    acc = 0
    nbits = 0
    for byte in data:
        code, ln = codes[byte]
        acc = (acc << ln) | code
        nbits += ln
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
            acc &= (1 << nbits) - 1
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)

    checksum = hashlib.sha256(data).digest()[:8]
    header = _HEADER.pack(MAGIC, checksum, len(data), len(lengths))
    table = bytearray()
    for sym in sorted(lengths):
        table += bytes((sym, lengths[sym]))
    return bytes(header + table + out)


# --------------------------------------------------------------------------
# Decoding (fast path)
# --------------------------------------------------------------------------

def _build_decode_tables(codes):
    """Build a lookup table indexed by the next _TABLE_WIDTH bits.

    Returns (table, width, long_codes) where table[i] is (symbol, length),
    0 marks the prefix of a code longer than `width`, and long_codes maps
    (length, code) -> symbol for those rare long codes.
    """
    width = min(max(ln for _, ln in codes.values()), _TABLE_WIDTH)
    table = [None] * (1 << width)
    long_codes = {}
    for sym, (code, ln) in codes.items():
        if ln <= width:
            base = code << (width - ln)
            for i in range(base, base + (1 << (width - ln))):
                table[i] = (sym, ln)
        else:
            prefix = code >> (ln - width)
            if table[prefix] is None:
                table[prefix] = 0  # marker: longer code follows
            long_codes[(ln, code)] = sym
    return table, width, long_codes


def _parse_container(buf):
    if len(buf) < _HEADER.size:
        raise TruncatedStreamError(
            "container too short: %d bytes" % len(buf))
    magic, checksum, orig_len, count = _HEADER.unpack_from(buf, 0)
    if magic != MAGIC:
        raise BadMagicError("bad magic: %r" % (magic,))
    table_end = _HEADER.size + 2 * count
    if len(buf) < table_end:
        raise TruncatedStreamError("code table truncated")
    lengths = {}
    prev_sym = -1
    for off in range(_HEADER.size, table_end, 2):
        sym, ln = buf[off], buf[off + 1]
        if sym <= prev_sym:
            raise InvalidCodeTableError("symbols not strictly increasing")
        prev_sym = sym
        lengths[sym] = ln
    if count and not lengths:
        raise InvalidCodeTableError("empty code table")
    if lengths:
        _validate_lengths(lengths)
    elif orig_len:
        raise InvalidCodeTableError("no code table but non-empty payload")
    return checksum, orig_len, lengths, buf[table_end:]


def decode(container):
    """Decode a container produced by encode(). Raises HuffmanError
    subclasses on any corruption; never returns partial data."""
    checksum, orig_len, lengths, payload = _parse_container(bytes(container))
    if orig_len == 0:
        if payload:
            raise TrailingBitsError("payload present for empty message")
        return b""

    codes = canonical_codes(lengths)
    table, width, long_codes = _build_decode_tables(codes)
    mask = (1 << width) - 1

    out = bytearray()
    buf = 0
    nbits = 0
    pos = 0
    plen = len(payload)
    remaining = orig_len
    while remaining:
        while nbits < width and pos < plen:
            buf = (buf << 8) | payload[pos]
            nbits += 8
            pos += 1
        idx = (buf >> (nbits - width)) & mask if nbits >= width \
            else (buf << (width - nbits)) & mask
        entry = table[idx]
        if entry is None:
            raise InvalidCodeTableError("undecodable bit pattern")
        if entry == 0:
            # Rare code longer than `width`: walk bit by bit.
            code = idx
            ln = width
            sym = None
            while ln < _MAX_CODE_LEN:
                if nbits <= ln:
                    if pos >= plen:
                        raise TruncatedStreamError(
                            "bitstream ends inside a symbol")
                    buf = (buf << 8) | payload[pos]
                    nbits += 8
                    pos += 1
                ln += 1
                code = (code << 1) | ((buf >> (nbits - ln)) & 1)
                sym = long_codes.get((ln, code))
                if sym is not None:
                    break
            if sym is None:
                raise InvalidCodeTableError("undecodable long code")
        else:
            sym, ln = entry
            if ln > nbits:
                raise TruncatedStreamError("bitstream ends inside a symbol")
        nbits -= ln
        buf &= (1 << nbits) - 1
        out.append(sym)
        remaining -= 1

    # After the last symbol only zero padding (< 1 byte) may remain.
    leftover_bits = nbits + 8 * (plen - pos)
    if leftover_bits >= 8:
        raise TrailingBitsError(
            "%d extra bits after end of message" % leftover_bits)
    if buf != 0 or any(payload[pos:]):
        raise TrailingBitsError("non-zero padding bits after end of message")

    if hashlib.sha256(bytes(out)).digest()[:8] != checksum:
        raise DataMismatchError(
            "decoded data does not match stored checksum")
    return bytes(out)
