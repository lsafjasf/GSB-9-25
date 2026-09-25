"""Bit-level cross-check ("dui pai") script.

Decodes every archive twice:
  1. with the library's optimized decoder (huffman.decompress), and
  2. with an independent, deliberately simple REFERENCE decoder that walks a
     binary trie one bit at a time.

Both outputs must match bit-for-bit, and the reference decoder must consume
exactly (total bits - padding) bits of the payload. It also re-compresses the
result and requires the re-encoded bitstream to be identical (determinism).

Run: python3 bitwise_check.py
"""

import random
import struct
import sys

import huffman


def reference_decompress(blob: bytes) -> bytes:
    """Independent bit-by-bit decoder; shares no code with the library."""
    assert blob[:4] == b"HUF1", "bad magic"
    orig_len, count = struct.unpack(">QH", blob[4:14])
    pos = 14
    lengths = {}
    for _ in range(count):
        sym, ln = blob[pos], blob[pos + 1]
        lengths[sym] = ln
        pos += 2
    padding = blob[pos]
    pos += 1
    pos += 16  # skip digest; the library verifies it
    payload = blob[pos:]

    # Rebuild canonical codes independently (sort by length, then symbol).
    trie = {}
    code = 0
    prev = 0
    for sym, ln in sorted(lengths.items(), key=lambda kv: (kv[1], kv[0])):
        code <<= ln - prev
        node = trie
        for i in range(ln - 1, -1, -1):
            bit = (code >> i) & 1
            node = node.setdefault(bit, {})
        node["sym"] = sym
        code += 1
        prev = ln

    out = bytearray()
    bits_consumed = 0
    total_bits = len(payload) * 8
    node = trie
    for i in range(total_bits):
        if len(out) == orig_len:
            break
        bit = (payload[i >> 3] >> (7 - (i & 7))) & 1
        bits_consumed += 1
        node = node[bit]
        if "sym" in node:
            out.append(node["sym"])
            node = trie
    assert len(out) == orig_len, "reference decoder ran out of bits"
    assert bits_consumed + padding == total_bits, (
        f"bit accounting off: consumed {bits_consumed}, padding {padding}, "
        f"total {total_bits}")
    return bytes(out)


def check(data: bytes, label: str) -> None:
    blob = huffman.compress(data)
    fast = huffman.decompress(blob)
    ref = reference_decompress(blob)
    assert fast == ref == data, f"{label}: decoder mismatch"
    assert huffman.compress(fast) == blob, f"{label}: re-encode not identical"
    print(f"PASS {label:<38} {len(data):>8} B -> {len(blob):>8} B")


def main() -> int:
    rng = random.Random(20260925)
    cases = [
        (b"", "empty"),
        (b"A", "single byte"),
        (b"\xff", "single high byte"),
        (b"\x00" * 7, "all same (7)"),
        (b"\x80" * 999, "all same high byte (999)"),
        (b"\x00\xff" * 128, "two symbols"),
        (bytes(range(256)), "all 256 symbols once"),
        (bytes(range(256)) * 33, "all 256 symbols x33"),
        (b"hello huffman world! " * 50, "text"),
        (rng.randbytes(1), "random 1B"),
        (rng.randbytes(13), "random 13B"),
        (rng.randbytes(1000), "random 1KB"),
        (rng.randbytes(100000), "random 100KB"),
        (bytes(rng.choices(range(256), weights=[1 << (i % 7) for i in range(256)],
                           k=50000)), "skewed 50KB"),
    ]
    for data, label in cases:
        check(data, label)
    print("all bit-level cross-checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
