"""CLI: python -m filededup [options] PATH [PATH ...]"""

from __future__ import annotations

import argparse
import resource
import sys
import time

from .cdc import Chunker
from .report import build_report, format_json, format_text
from .scanner import DedupIndex


def _peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="filededup",
        description="File fingerprint + content-defined-chunk duplicate detection",
    )
    ap.add_argument("paths", nargs="+", help="files and/or directories to scan")
    ap.add_argument(
        "--min-sim",
        type=float,
        default=0.5,
        help="shared-chunk containment threshold for similar groups (default 0.5)",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--min-size", type=int, default=4 * 1024, help="min chunk bytes")
    ap.add_argument("--avg-size", type=int, default=16 * 1024, help="avg chunk bytes")
    ap.add_argument("--max-size", type=int, default=64 * 1024, help="max chunk bytes")
    args = ap.parse_args(argv)

    chunker = Chunker(args.min_size, args.avg_size, args.max_size)
    index = DedupIndex(chunker)
    t0 = time.perf_counter()
    index.add_paths(args.paths)
    elapsed = time.perf_counter() - t0

    report = build_report(index, args.min_sim, elapsed)
    out = format_json(report) if args.json else format_text(report)
    print(out)
    print(f"\n[peak RSS: {_peak_rss_mib():.1f} MiB]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
