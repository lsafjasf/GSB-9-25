#!/usr/bin/env python3
"""Size and speed comparison across data distributions.

Usage:  python3 benchmark_sizes.py

Prints, for representative distributions:
  original size, chosen mode (RAW/LZ), frame size, ratio vs original,
  and the size an always-compressing frame would have had;
then a timing table comparing the current hash-chain encoder against the
pre-fix full backward scan (kept below as ``_lz_encode_scan``), including
a few-KB incompressible block -- the input class that used to be slowest.
"""

import os
import time

from mini_compress import compress, HEADER_SIZE, MODE_RAW, MODE_LZ
from mini_compress.mini_compress import (
    _lz_encode,
    MAX_LITERAL,
    MAX_MATCH_ADD,
    MAX_OFFSET,
)


def _lz_encode_scan(data: bytes) -> bytearray:
    """Pre-fix encoder: rescans every earlier position at each step.

    Kept verbatim (from git history) as the before/after reference; the
    fixed encoder must emit byte-identical tokens, just faster.
    """
    payload = bytearray()
    n = len(data)
    i = 0
    while i < n:
        best_len = 0
        best_off = 0
        if i + 3 <= n:
            limit = min(n - i, MAX_MATCH_ADD + 3)
            for p in range(i - 1, -1, -1):
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


SAMPLES = [
    ("empty", b""),
    ("1 byte", b"\x00"),
    ("2 bytes", b"\x00\x01"),
    ("repeat 0x00 x100", b"\x00" * 100),
    ("repeat 0xFF x1000", b"\xff" * 1000),
    ("periodic text x100", b"abcdefgh" * 100),
    ("period-3 x300", b"abc" * 300),
    ("low alphabet 4 x2000", bytes([i & 3 for i in range(2000)])),
    ("high-bit bytes x200", bytes([0x80 | (i & 0x7F) for i in range(200)])),
    ("sawtooth 0..255 x4", (bytes(range(256)) * 4)),
    ("all 256 byte values", bytes(range(256))),
    ("text-like 400B", (b"The quick brown fox jumps over the lazy dog. " * 8)),
    ("pseudo-random 512B", bytes(
        ((i * 1103515245 + 12345) >> 8) & 0xFF for i in range(512))),
    ("os.urandom 512B", os.urandom(512)),
]

# Timing samples: same distribution families, larger sizes, and crucially
# a few-KB incompressible block (the old quadratic worst case).
TIMING_SAMPLES = [
    ("incompressible 4KB (urandom)", os.urandom(4096)),
    ("incompressible 8KB (urandom)", os.urandom(8192)),
    ("incompressible 64KB (urandom)", os.urandom(65536)),
    ("pseudo-random 8KB", bytes(
        ((i * 1103515245 + 12345) >> 8) & 0xFF for i in range(8192))),
    ("text-like 32KB", (b"The quick brown fox jumps over the lazy dog. " * 728)),
    ("periodic 64KB", b"abcdefgh" * 8192),
    ("zero run 64KB", b"\x00" * 65536),
    ("low alphabet 64KB", bytes([i & 3 for i in range(65536)])),
]


def bench(fn, data, repeat=3):
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn(data)
        best = min(best, time.perf_counter() - t0)
    return best


def main():
    print("%-24s %8s %5s %8s %7s %8s"
          % ("distribution", "orig", "mode", "frame", "ratio", "lz-only"))
    print("-" * 70)
    for name, data in SAMPLES:
        frame = compress(data)
        mode = "RAW" if frame[4] == MODE_RAW else "LZ"
        lz_only = HEADER_SIZE + len(_lz_encode(data))
        ratio = len(frame) / len(data) if data else float("inf")
        ratio_s = "inf" if not data else "%.3f" % ratio
        print("%-24s %8d %5s %8d %7s %8d"
              % (name, len(data), mode, len(frame), ratio_s, lz_only))
    print("-" * 70)
    print("Worst-case absolute overhead is bounded: frame = orig + %d bytes "
          "(RAW)." % HEADER_SIZE)

    print()
    print("%-30s %8s %12s %12s %9s"
          % ("distribution", "orig", "old scan", "fixed", "speedup"))
    print("-" * 76)
    for name, data in TIMING_SAMPLES:
        old_out = bytes(_lz_encode_scan(data))
        new_out = bytes(_lz_encode(data))
        assert old_out == new_out, "encoder outputs diverged on %r" % name
        t_old = bench(_lz_encode_scan, data)
        t_new = bench(_lz_encode, data)
        print("%-30s %8d %10.1f ms %10.1f ms %8.1fx"
              % (name, len(data), t_old * 1e3, t_new * 1e3, t_old / t_new))
    print("-" * 76)
    print("Token streams asserted byte-identical; only the search differs.")


if __name__ == "__main__":
    main()
