"""Human-readable and JSON detection reports."""

from __future__ import annotations

import json

from .scanner import DedupIndex

MAX_LISTED = 20  # cap paths/edges listed per group in text output


def _fmt_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def build_report(index: DedupIndex, threshold: float, elapsed: float | None) -> dict:
    exact = index.exact_groups()
    similar = index.similar_groups(threshold)
    repeats = index.internal_repeats()
    report = {
        "summary": {
            "files": len(index.files),
            "bytes": index.bytes_scanned,
            "distinct_chunks": len(index.chunk_files),
            "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
            "exact_duplicate_groups": len(exact),
            "similar_groups": len(similar),
            "files_with_internal_repeats": len(repeats),
        },
        "exact_groups": [
            {
                "sha256": index.files[g[0]].digest,
                "size": index.files[g[0]].size,
                "count": len(g),
                "wasted_bytes": index.files[g[0]].size * (len(g) - 1),
                "paths": [index.files[i].path for i in g],
            }
            for g in exact
        ],
        "similar_groups": [
            {
                "members": [index.files[i].path for i in g.members],
                "edges": [
                    {
                        "a": index.files[e.file_a].path,
                        "b": index.files[e.file_b].path,
                        "shared_chunks": e.shared_chunks,
                        "containment": round(e.containment, 4),
                        "jaccard": round(e.jaccard, 4),
                    }
                    for e in g.edges
                ],
            }
            for g in similar
        ],
        "internal_repeats": [
            {
                "path": index.files[i].path,
                "distinct_repeated_chunks": k,
                "reclaimable_bytes": saved,
            }
            for i, k, saved in repeats
        ],
    }
    return report


def format_text(report: dict) -> str:
    out: list[str] = []
    s = report["summary"]
    out.append("== Summary ==")
    out.append(f"files scanned:            {s['files']}")
    out.append(f"bytes scanned:            {_fmt_size(s['bytes'])}")
    out.append(f"distinct chunks indexed:  {s['distinct_chunks']}")
    if s["elapsed_seconds"] is not None and s["elapsed_seconds"] > 0:
        mb = s["bytes"] / 1e6
        out.append(
            f"elapsed:                  {s['elapsed_seconds']:.2f} s"
            f"  ({mb / s['elapsed_seconds']:.1f} MB/s)"
        )
    out.append(f"exact duplicate groups:   {s['exact_duplicate_groups']}")
    out.append(f"similar groups:           {s['similar_groups']}")
    out.append(f"files w/ internal repeats:{s['files_with_internal_repeats']:>2}")

    out.append("")
    out.append(f"== Exact duplicate groups ({s['exact_duplicate_groups']}) ==")
    for n, g in enumerate(report["exact_groups"], 1):
        out.append(
            f"[exact {n}] sha256={g['sha256'][:16]}... size={_fmt_size(g['size'])}"
            f" x{g['count']}  (wasted {_fmt_size(g['wasted_bytes'])})"
        )
        for p in g["paths"][:MAX_LISTED]:
            out.append(f"    {p}")
        if len(g["paths"]) > MAX_LISTED:
            out.append(f"    ... and {len(g['paths']) - MAX_LISTED} more")

    out.append("")
    out.append(f"== Similar groups ({s['similar_groups']}) ==")
    for n, g in enumerate(report["similar_groups"], 1):
        best = g["edges"][0]
        out.append(
            f"[similar {n}] {len(g['members'])} files,"
            f" best match {best['containment'] * 100:.1f}% shared chunks"
        )
        for e in g["edges"][:MAX_LISTED]:
            out.append(
                f"    {e['containment'] * 100:6.2f}% shared"
                f" (jaccard {e['jaccard']:.3f}, {e['shared_chunks']} chunks)"
                f"  {e['a']}  <->  {e['b']}"
            )
        if len(g["edges"]) > MAX_LISTED:
            out.append(f"    ... and {len(g['edges']) - MAX_LISTED} more pairs")

    out.append("")
    out.append(f"== Files with internal repeated chunks ({s['files_with_internal_repeats']}) ==")
    for r in report["internal_repeats"][:MAX_LISTED]:
        out.append(
            f"    {r['path']}: {r['distinct_repeated_chunks']} repeated chunk types,"
            f" ~{_fmt_size(r['reclaimable_bytes'])} reclaimable"
        )
    if len(report["internal_repeats"]) > MAX_LISTED:
        out.append(f"    ... and {len(report['internal_repeats']) - MAX_LISTED} more")
    return "\n".join(out)


def format_json(report: dict) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False)
