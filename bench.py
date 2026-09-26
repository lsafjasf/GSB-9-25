"""Scale benchmark for filededup.

Usage:
  python3 bench.py gen  --dir CORPUS                 # 10k files, ~2 GiB mixed corpus
  python3 bench.py run  --dir CORPUS                 # scan + report timing/memory
  python3 bench.py big  --path BIG.bin --gib 1       # single big file, O(1) memory proof
"""

from __future__ import annotations

import argparse
import os
import random
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from filededup import Chunker, DedupIndex
from filededup.report import build_report, format_text


def peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def human(n: float) -> str:
    for u in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or u == "GiB":
            return f"{n:.1f} {u}"
        n /= 1024


# ----------------------------------------------------------------- gen

def gen_corpus(directory: str, seed: int = 7) -> None:
    rng = random.Random(seed)
    os.makedirs(directory, exist_ok=True)

    def w(rel: str, data: bytes) -> None:
        p = os.path.join(directory, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)

    total = 0
    # 1) exact-duplicate sets: 100 sets x 10 copies of 64 KiB
    for s in range(100):
        blob = rng.randbytes(64 * 1024)
        for c in range(10):
            w(f"exact/set{s:03d}/copy{c}.bin", blob)
        total += 64 * 1024 * 10

    # 2) near-duplicate families: 400 bases x 8 members of ~512 KiB
    for f in range(400):
        base = bytearray(rng.randbytes(512 * 1024))
        for m in range(8):
            if m == 0:
                w(f"families/f{f:03d}/m{m}.bin", bytes(base))
                total += len(base)
                continue
            doc = bytearray(base)
            for _ in range(rng.randint(1, 3)):  # small random edits
                pos = rng.randrange(len(doc))
                if rng.random() < 0.5:
                    doc[pos:pos] = rng.randbytes(rng.randint(1024, 8192))
                else:
                    del doc[pos:pos + rng.randint(1024, 8192)]
            w(f"families/f{f:03d}/m{m}.bin", bytes(doc))
            total += len(doc)

    # 3) unique random files: 5500 x 64 KiB
    for u in range(5500):
        w(f"unique/u{u:04d}.bin", rng.randbytes(64 * 1024))
        total += 64 * 1024

    # 4) files with internal repeats: 180 files, a 128 KiB block repeated
    for r in range(180):
        block = rng.randbytes(128 * 1024)
        data = block + rng.randbytes(32 * 1024) + block + rng.randbytes(8 * 1024) + block
        w(f"repeats/r{r:03d}.bin", data)
        total += len(data)

    # 5) edge cases: empty + tiny files
    for e in range(20):
        w(f"edge/empty{e}.bin", b"")
    for t in range(100):
        n = rng.randint(1, 500)
        w(f"edge/tiny{t}.bin", rng.randbytes(n))
        total += n

    n_files = sum(len(fs) for _, _, fs in os.walk(directory))
    print(f"corpus: {n_files} files, {human(total)} payload in {directory}")


# ----------------------------------------------------------------- run

def run_scan(directory: str) -> None:
    index = DedupIndex(Chunker())
    rss0 = peak_rss_mib()
    t0 = time.perf_counter()
    index.add_paths([directory])
    t1 = time.perf_counter()
    report = build_report(index, threshold=0.5, elapsed=t1 - t0)
    t2 = time.perf_counter()
    print(format_text(report))
    s = report["summary"]
    print()
    print(f"scan phase:    {t1 - t0:.1f} s "
          f"({s['bytes'] / 1e6 / (t1 - t0):.1f} MB/s)")
    print(f"group+report:  {t2 - t1:.1f} s")
    print(f"peak RSS:      {peak_rss_mib():.0f} MiB (start {rss0:.0f} MiB)")


# ----------------------------------------------------------------- big

def big_file(path: str, gib: float) -> None:
    if not os.path.exists(path):
        print(f"generating {path} ({gib} GiB of random data)...")
        rng = random.Random(1)
        with open(path, "wb") as fh:
            for _ in range(int(gib * 16)):
                fh.write(rng.randbytes(64 * 1024 * 1024))
    size = os.path.getsize(path)
    index = DedupIndex(Chunker())
    rss0 = peak_rss_mib()
    t0 = time.perf_counter()
    index.add_paths([path])
    dt = time.perf_counter() - t0
    f = index.files[0]
    print(f"file:          {path} ({human(size)})")
    print(f"chunks:        {f.n_chunks} (avg {human(size / max(f.n_chunks, 1))})")
    print(f"index entries: {len(index.chunk_files)} distinct chunks")
    print(f"scan time:     {dt:.1f} s ({size / 1e6 / dt:.1f} MB/s)")
    print(f"peak RSS:      {peak_rss_mib():.0f} MiB (before scan {rss0:.0f} MiB)")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--dir", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dir", required=True)
    b = sub.add_parser("big")
    b.add_argument("--path", required=True)
    b.add_argument("--gib", type=float, default=1.0)
    args = ap.parse_args()
    if args.cmd == "gen":
        gen_corpus(args.dir)
    elif args.cmd == "run":
        run_scan(args.dir)
    else:
        big_file(args.path, args.gib)


if __name__ == "__main__":
    main()
