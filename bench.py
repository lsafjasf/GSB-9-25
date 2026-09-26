"""Benchmark: 100,000 format calls per scenario, stdlib only."""

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from timespan import TimeSpanFormatter

LOCALES = Path(__file__).resolve().parent / "timespan" / "locales"
N = 100_000


def bench(name, fmt, samples, **kwargs):
    start = time.perf_counter()
    for s in samples:
        fmt.format(s, **kwargs)
    elapsed = time.perf_counter() - start
    print(f"{name:<38} {N:>7,} calls  {elapsed:6.3f}s  {elapsed / N * 1e6:7.2f} us/call")
    return elapsed


def main():
    rng = random.Random(20260926)
    mixed = [rng.uniform(-10**7, 10**7) for _ in range(N)]
    small = [rng.uniform(0, 120) for _ in range(N)]

    zh = TimeSpanFormatter.from_locale_file(LOCALES / "zh.json")
    en = TimeSpanFormatter.from_locale_file(LOCALES / "en.json")

    print(f"Python {sys.version.split()[0]}, {N:,} calls per scenario\n")
    total = 0.0
    total += bench("zh truncate precision=2 (mixed spans)", zh, mixed)
    total += bench("zh round    precision=2 (mixed spans)", zh, mixed, mode="round")
    total += bench("zh truncate precision=4 (mixed spans)", zh, mixed, precision=4)
    total += bench("en truncate precision=2 (mixed spans)", en, mixed)
    total += bench("zh truncate precision=2 (small spans)", zh, small)
    print(f"\n{'total':<38} {5 * N:>7,} calls  {total:6.3f}s")


if __name__ == "__main__":
    main()
