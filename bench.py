"""超长输入性能基准。

运行：python3 bench.py [最大MiB=16]
"""

import random
import sys
import time

from strict_hex import hex_decode, hex_encode
from strict_b64 import b64_decode, b64_encode


def bench(name, fn, arg, size_bytes, repeat=3):
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn(arg)
        dt = time.perf_counter() - t0
        best = min(best, dt)
    mbps = size_bytes / best / (1024 * 1024)
    print("  %-22s %8.3f s   %8.1f MiB/s" % (name, best, mbps))
    return result


def main():
    max_mib = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    rng = random.Random(7)
    sizes = [mib for mib in (1, 4, 16, 64) if mib <= max_mib]
    for mib in sizes:
        n = mib * 1024 * 1024
        raw = rng.randbytes(n)
        print("== 输入 %d MiB（%d 字节）==" % (mib, n))
        h = bench("hex_encode", hex_encode, raw, n)
        raw2 = bench("hex_decode", hex_decode, h, n)
        assert raw2 == raw
        b = bench("b64_encode(标准+填充)", b64_encode, raw, n)
        raw3 = bench("b64_decode(标准+填充)", b64_decode, b, n)
        assert raw3 == raw
        bu = b64_encode(raw, urlsafe=True, padding=False)
        raw4 = bench("b64_decode(URL+无填充)",
                     lambda s: b64_decode(s, urlsafe=True, require_padding=False), bu, n)
        assert raw4 == raw
        print("  往返一致性: OK")
        print()


if __name__ == "__main__":
    main()
