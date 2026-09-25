"""Throughput + compression-ratio benchmark on 10 MiB of mixed content.

Mix: 4 MiB English-like text, 3 MiB structured binary, 3 MiB random bytes.
Run with:  python3 benchmark.py
"""

import random
import time

import huffman

MIB = 1 << 20


def make_text(n):
    words = ("the quick brown fox jumps over a lazy dog compression archive "
             "entropy prefix code deterministic symbol frequency stream "
             "buffer decode encode table checksum binary data ").split()
    rng = random.Random(1)
    parts = []
    total = 0
    while total < n:
        w = rng.choice(words)
        parts.append(w)
        total += len(w) + 1
    return (" ".join(parts)).encode()[:n]


def make_binary(n):
    # Structured binary: record-like patterns, counters, runs of zeros.
    out = bytearray()
    rec = bytes(range(64))
    i = 0
    while len(out) < n:
        out += rec
        out += (i & 0xFFFF).to_bytes(2, "big") * 8
        out += b"\x00" * 48
        i += 1
    return bytes(out[:n])


def _time(fn):
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def main():
    text = make_text(4 * MIB)
    binary = make_binary(3 * MIB)
    rnd = random.Random(2).randbytes(3 * MIB)
    data = text + binary + rnd
    assert len(data) == 10 * MIB

    print("input: 10 MiB mixed (4 MiB text + 3 MiB binary + 3 MiB random)")

    enc_t = min(_time(lambda: huffman.encode(data)) for _ in range(3))
    container = huffman.encode(data)
    dec_t = min(_time(lambda: huffman.decode(container)) for _ in range(3))

    assert huffman.decode(container) == data, "roundtrip failed"
    print("roundtrip: OK  (timings are best of 3 runs)")

    print()
    print("section ratios (compressed / raw, fixed-length 8bit/byte = 1.000):")
    for name, part in (("text  ", text), ("binary", binary),
                       ("random", rnd), ("TOTAL ", data)):
        c = huffman.encode(part)
        print("  %s raw=%9d  compressed=%9d  ratio=%.4f"
              % (name, len(part), len(c), len(c) / len(part)))

    print()
    print("overall: raw=%d bytes -> %d bytes (ratio %.4f)"
          % (len(data), len(container), len(container) / len(data)))
    print("encode: %.2f s  ->  %.2f MiB/s" % (enc_t, 10 / enc_t))
    print("decode: %.2f s  ->  %.2f MiB/s" % (dec_t, 10 / dec_t))


if __name__ == "__main__":
    main()
