#!/usr/bin/env python3
"""Size comparison across data distributions.

Usage:  python3 benchmark_sizes.py

Prints, for representative distributions:
  original size, chosen mode (RAW/LZ), frame size, ratio vs original,
  and the size an always-compressing frame would have had.
"""

import os

from mini_compress import compress, HEADER_SIZE, MODE_RAW, MODE_LZ
from mini_compress.mini_compress import _lz_encode


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


def main():
    print("%-24s %8s %5s %8s %7s %8s"
          % ("distribution", "orig", "mode", "frame", "ratio", "lz-only"))
    print("-" * 70)
    worst = 0.0
    for name, data in SAMPLES:
        frame = compress(data)
        mode = "RAW" if frame[4] == MODE_RAW else "LZ"
        lz_only = HEADER_SIZE + len(_lz_encode(data))
        ratio = len(frame) / len(data) if data else float("inf")
        worst = max(worst, (len(frame) - len(data)) / max(1, len(data)))
        ratio_s = "inf" if not data else "%.3f" % ratio
        print("%-24s %8d %5s %8d %7s %8d"
              % (name, len(data), mode, len(frame), ratio_s, lz_only))
    print("-" * 70)
    print("Worst-case absolute overhead is bounded: frame = orig + %d bytes "
          "(RAW)." % HEADER_SIZE)


if __name__ == "__main__":
    main()
