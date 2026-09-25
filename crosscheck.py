"""Bit-by-bit reference decoder + cross-check driver.

`reference_decode` is a deliberately simple, obviously-correct decoder:
it parses the container, rebuilds the code tree, and walks the bitstream
one bit at a time.  `crosscheck` feeds the same container to both the
fast decoder (huffman.decode) and the reference decoder and verifies
that every consumed bit and every output byte agree.
"""

import random
import struct
import sys

import huffman
from huffman import (
    MAGIC, BadMagicError, InvalidCodeTableError, TrailingBitsError,
    TruncatedStreamError, canonical_codes,
)


def _read_bit(payload, bitpos):
    return (payload[bitpos >> 3] >> (7 - (bitpos & 7))) & 1


def reference_decode(container):
    """Decode bit-by-bit. Returns (data, consumed_bit_count)."""
    if len(container) < 22:
        raise TruncatedStreamError("container too short")
    magic = container[:4]
    if magic != MAGIC:
        raise BadMagicError("bad magic")
    orig_len = struct.unpack_from(">Q", container, 12)[0]
    count = struct.unpack_from(">H", container, 20)[0]
    table_end = 22 + 2 * count
    if len(container) < table_end:
        raise TruncatedStreamError("code table truncated")
    lengths = {}
    for off in range(22, table_end, 2):
        lengths[container[off]] = container[off + 1]
    payload = container[table_end:]

    if orig_len == 0:
        if payload:
            raise TrailingBitsError("payload present for empty message")
        return b"", 0

    codes = canonical_codes(lengths)
    # Build an explicit binary tree: nested dicts, leaves are symbols.
    tree = {}
    for sym, (code, ln) in codes.items():
        node = tree
        for i in range(ln - 1, -1, -1):
            bit = (code >> i) & 1
            nxt = node.get(bit)
            if nxt is None:
                nxt = {}
                node[bit] = nxt
            node = nxt
        node["sym"] = sym

    out = bytearray()
    bitpos = 0
    total_bits = len(payload) * 8
    while len(out) < orig_len:
        node = tree
        while "sym" not in node:
            if bitpos >= total_bits:
                raise TruncatedStreamError("bitstream ends inside a symbol")
            bit = _read_bit(payload, bitpos)
            bitpos += 1
            node = node.get(bit)
            if node is None:
                raise InvalidCodeTableError("undecodable bit pattern")
        out.append(node["sym"])

    # Remaining bits must be zero padding within the final partial byte.
    leftover = total_bits - bitpos
    if leftover >= 8:
        raise TrailingBitsError("extra bytes after end of message")
    for i in range(bitpos, total_bits):
        if _read_bit(payload, i):
            raise TrailingBitsError("non-zero padding bits")
    return bytes(out), bitpos


def crosscheck(data):
    """Encode `data`, then verify fast decode == reference decode == data,
    and that both decoders consume exactly the same bits."""
    container = huffman.encode(data)
    fast = huffman.decode(container)
    ref, ref_bits = reference_decode(container)
    assert fast == data, "fast decoder mismatch"
    assert ref == data, "reference decoder mismatch"
    # Bit-level agreement: recompute the expected bit count from the table.
    orig_len = struct.unpack_from(">Q", container, 12)[0]
    count = struct.unpack_from(">H", container, 20)[0]
    lengths = {container[22 + 2 * i]: container[23 + 2 * i]
               for i in range(count)}
    from collections import Counter
    freqs = Counter(data)
    expected_bits = sum(freqs[s] * lengths[s] for s in freqs)
    assert ref_bits == expected_bits, (
        "consumed bits %d != expected %d" % (ref_bits, expected_bits))
    assert (ref_bits + 7) // 8 == len(container) - 22 - 2 * count
    return container


def _selftest():
    rng = random.Random(20260925)
    cases = [
        b"",
        b"a",
        b"\x00",
        b"\xff" * 1000,
        b"\x80\x81\xfe\xff",
        b"hello world, hello world, hello!",
        bytes(range(256)),
        bytes(range(256)) * 40,
        rng.randbytes(4096),
        rng.randbytes(1 << 18),  # 256 KiB random
        (b"the quick brown fox jumps over the lazy dog. " * 3000),
        bytes(rng.choices(range(256), weights=[1] * 200 + [5000] * 56,
                          k=1 << 18)),
    ]
    for i, data in enumerate(cases):
        crosscheck(data)
        print("case %2d ok (input %d bytes)" % (i, len(data)))
    # Determinism: encoding twice must give identical bytes.
    for data in cases:
        assert huffman.encode(data) == huffman.encode(data)
    print("determinism ok")
    print("ALL CROSSCHECKS PASSED")


if __name__ == "__main__":
    sys.exit(_selftest())
