"""Compression-ratio and throughput benchmark.

Builds a 10 MiB mixed payload (text + structured binary + random bytes),
measures compress/decompress time, and compares against fixed-length
encoding. Also runs a pure-random payload to show the expansion case
(already-compressed / incompressible data gets larger).

Run: python3 benchmark.py
"""

import math
import random
import time

import huffman

MIB = 1024 * 1024


def make_text(rng: random.Random, n: int) -> bytes:
    words = ("the quick brown fox jumps over lazy dog compression archive "
             "huffman entropy symbol code table bitstream deterministic "
             "and of to in a is that for on with as are was").split()
    out = bytearray()
    while len(out) < n:
        out += " ".join(rng.choices(words, k=40)).encode() + b"\n"
    return bytes(out[:n])


def make_binary(rng: random.Random, n: int) -> bytes:
    out = bytearray()
    record = 0
    while len(out) < n:
        # Structured records: low-entropy counters and flags.
        out += bytes((record & 0xFF, (record >> 8) & 0xFF, 0x00, 0x01,
                      0xDE, 0xAD, record % 7, 0x00))
        record += 1
    return bytes(out[:n])


def entropy_bits_per_byte(data: bytes) -> float:
    freq = [0] * 256
    for b in data:
        freq[b] += 1
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in freq if c)


def report(label: str, data: bytes) -> None:
    t0 = time.perf_counter()
    blob = huffman.compress(data)
    t1 = time.perf_counter()
    back = huffman.decompress(blob)
    t2 = time.perf_counter()
    assert back == data, "round-trip failed"
    assert huffman.compress(data) == blob, "non-deterministic output"

    n = len(data)
    alphabet = len(set(data))
    fixed_bits = max(1, math.ceil(math.log2(max(alphabet, 2))))
    ent = entropy_bits_per_byte(data)
    ratio = len(blob) / n
    huff_bpb = (len(blob) * 8) / n
    c_mb = n / MIB / (t1 - t0)
    d_mb = n / MIB / (t2 - t1)
    print(f"[{label}]")
    print(f"  size            : {n / MIB:8.2f} MiB -> {len(blob) / MIB:8.2f} MiB"
          f"   (ratio {ratio:.4f}, {100 * (1 - ratio):+.1f}%)")
    print(f"  alphabet        : {alphabet:3d} symbols, entropy {ent:.3f} bit/byte")
    print(f"  bits/byte       : huffman {huff_bpb:.3f} (incl. header+table)"
          f"  vs fixed-length {fixed_bits}  vs raw 8")
    print(f"  compress        : {t1 - t0:7.2f} s  ({c_mb:6.2f} MiB/s)")
    print(f"  decompress      : {t2 - t1:7.2f} s  ({d_mb:6.2f} MiB/s)")
    print()


def main() -> None:
    rng = random.Random(20260925)
    text = make_text(rng, 4 * MIB)
    binary = make_binary(rng, 3 * MIB)
    noise = rng.randbytes(3 * MIB)
    mixed = text + binary + noise
    print(f"mixed payload: {len(mixed) / MIB:.2f} MiB "
          f"(4 MiB text + 3 MiB structured binary + 3 MiB random)\n")
    report("mixed 10 MiB", mixed)
    report("pure random 10 MiB (already-compressed stand-in)",
           rng.randbytes(10 * MIB))


if __name__ == "__main__":
    main()
