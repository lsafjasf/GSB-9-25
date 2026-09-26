#!/usr/bin/env python3
"""Size comparison across data distributions: raw vs v1 vs fixed v2.

Prints a markdown table.  Deterministic inputs only.
Run: python3 bench_size.py
"""

from mini_compress import compress2 as c2
from mini_compress import legacy


def lcg_bytes(n, seed=0x1234):
    x = seed
    out = bytearray()
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0xFFFFFFFF
        out.append(((x >> 16) & 0xFF) ^ (x & 0xFF))
    return bytes(out)


def row(name, data):
    v1 = legacy.compress(data) if len(data) <= 0xFFFF else b""
    v2 = c2.compress(data)
    mode = "DEFLATE" if v2[4] & 1 else "STORED"
    v1_cell = "%d (%+.1f%%)" % (
        len(v1), 100.0 * (len(v1) - len(data)) / max(1, len(data)))
    v2_cell = "%d (%+.1f%%)" % (
        len(v2), 100.0 * (len(v2) - len(data)) / max(1, len(data)))
    return "| %-22s | %5d | %-17s | %-17s |" % (
        name, len(data), v1_cell, v2_cell + " " + mode)


def main():
    rows = [
        ("empty", b""),
        ("1 byte", b"\x00"),
        ("2 bytes", b"\xab\xcd"),
        ("repeated byte x64", b"A" * 64),
        ("repeated byte x1000", b"\x00" * 1000),
        ("periodic (ab x 500)", b"ab" * 500),
        ("high-bit ramp x4", bytes(range(0x80, 0x100)) * 4),
        ("english-like text",
         (b"the quick brown fox jumps over the lazy dog. " * 12)[:1000]),
        ("pseudo-random 32B", lcg_bytes(32)),
        ("pseudo-random 200B", lcg_bytes(200)),
        ("pseudo-random 1000B", lcg_bytes(1000)),
    ]
    print("| distribution           | raw   | v1 (legacy)       | v2 (fixed)          |")
    print("|------------------------|------:|------------------:|--------------------:|")
    for name, data in rows:
        print(row(name, data))


if __name__ == "__main__":
    main()
