"""Benchmark: parse + apply a patch with 10,000+ hunks (stdlib only)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from linepatch import ApplyOptions, apply_to_text, parse_patch


def build_large_case(n_hunks: int, gap: int = 20, context: int = 3):
    """Original file + patch with `n_hunks` single-line-change hunks."""
    lines_per_hunk = gap
    total = n_hunks * lines_per_hunk + context + 1
    original = [f"line {i:08d} original content" for i in range(total)]
    patch_lines = ["--- a/big.txt", "+++ b/big.txt"]
    for h in range(n_hunks):
        target = h * lines_per_hunk + gap // 2  # 0-based index of changed line
        start = target - context               # 0-based hunk start
        old_start = start + 1                  # 1-based for the header
        count = 2 * context + 1
        patch_lines.append(f"@@ -{old_start},{count} +{old_start},{count} @@")
        for i in range(start, start + count):
            if i == target:
                patch_lines.append("-" + original[i])
                patch_lines.append(f"+line {i:08d} MODIFIED by hunk {h}")
            else:
                patch_lines.append(" " + original[i])
    return original, "\n".join(patch_lines) + "\n"


def main():
    n_hunks = int(sys.argv[1]) if len(sys.argv) > 1 else 12_000
    original, patch_text = build_large_case(n_hunks)
    content = "\n".join(original) + "\n"
    print(f"hunks: {n_hunks}")
    print(f"target file: {len(original)} lines, {len(content) / 1e6:.1f} MB")
    print(f"patch text:  {patch_text.count(chr(10))} lines, "
          f"{len(patch_text) / 1e6:.1f} MB")

    t0 = time.perf_counter()
    patch = parse_patch(patch_text)
    t1 = time.perf_counter()
    assert patch.hunk_count == n_hunks
    print(f"parse: {t1 - t0:.3f}s  ({n_hunks / (t1 - t0):,.0f} hunks/s)")

    t2 = time.perf_counter()
    out, reports = apply_to_text(content, patch.files[0])
    t3 = time.perf_counter()
    assert "MODIFIED by hunk 0" in out and "MODIFIED by hunk %d" % (n_hunks - 1) in out
    assert all(r.offset == 0 for r in reports)
    print(f"apply: {t3 - t2:.3f}s  ({n_hunks / (t3 - t2):,.0f} hunks/s)")

    # apply with every hunk shifted by +5 lines (offset search path)
    shifted = ["pad"] * 5 + original
    t4 = time.perf_counter()
    out2, reports2 = apply_to_text("\n".join(shifted) + "\n", patch.files[0],
                                   ApplyOptions(max_offset=10))
    t5 = time.perf_counter()
    assert all(r.offset == 5 for r in reports2)
    print(f"apply with +5-line drift: {t5 - t4:.3f}s  "
          f"({n_hunks / (t5 - t4):,.0f} hunks/s)")
    print(f"total: {t5 - t0:.3f}s")


if __name__ == "__main__":
    main()
