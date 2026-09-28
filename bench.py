"""Benchmark: parse + apply + reverse-apply a patch with 10k+ hunks.

Run: python3 bench.py [n_hunks]
"""
import sys
import time

from linepatch import apply_file_patch, parse_patch, reverse_patch


def make_case(n_hunks, ctx=3):
    """Build (old_content, patch_text, new_content) with n_hunks hunks.

    Layout: blocks of 8 lines; the last line of each block is modified.
    Hunks use `ctx` context lines and never overlap (block size 8 > 2*ctx+1).
    """
    old_lines = []
    for k in range(n_hunks):
        for j in range(7):
            old_lines.append("block-%06d filler-%d" % (k, j))
        old_lines.append("block-%06d OLD" % k)
    old_lines.extend("tail filler-%d" % j for j in range(ctx))
    new_lines = list(old_lines)
    hunks = []
    for k in range(n_hunks):
        i = 8 * k + 7  # 0-based index of the changed line
        new_lines[i] = "block-%06d NEW" % k
        start = i - ctx
        hunk = ["@@ -%d,7 +%d,7 @@" % (start + 1, start + 1)]
        for j in range(start, i):
            hunk.append(" " + old_lines[j])
        hunk.append("-" + old_lines[i])
        hunk.append("+" + new_lines[i])
        for j in range(i + 1, i + 1 + ctx):
            hunk.append(" " + old_lines[j])
        hunks.append("\n".join(hunk) + "\n")
    patch_text = "--- a/big.txt\n+++ b/big.txt\n" + "".join(hunks)
    return ("\n".join(old_lines) + "\n", patch_text,
            "\n".join(new_lines) + "\n")


def main():
    n_hunks = int(sys.argv[1]) if len(sys.argv) > 1 else 12000
    old, patch_text, expected_new = make_case(n_hunks)
    print("hunks:            %d" % n_hunks)
    print("patch size:       %.2f MB" % (len(patch_text) / 1e6))
    print("target file size: %.2f MB (%d lines)"
          % (len(old) / 1e6, old.count("\n")))

    t0 = time.perf_counter()
    patch = parse_patch(patch_text)
    t1 = time.perf_counter()
    assert len(patch.files) == 1 and len(patch.files[0].hunks) == n_hunks
    print("parse:            %.3f s  (%.0f hunks/s)" % (t1 - t0, n_hunks / (t1 - t0)))

    t0 = time.perf_counter()
    new = apply_file_patch(patch.files[0], old)
    t1 = time.perf_counter()
    assert new == expected_new, "forward apply produced wrong content"
    print("apply forward:    %.3f s  (%.0f hunks/s)" % (t1 - t0, n_hunks / (t1 - t0)))

    rev = reverse_patch(patch)
    t0 = time.perf_counter()
    restored = apply_file_patch(rev.files[0], new)
    t1 = time.perf_counter()
    assert restored == old, "reverse apply did not restore original"
    print("apply reverse:    %.3f s  (%.0f hunks/s)" % (t1 - t0, n_hunks / (t1 - t0)))
    print("roundtrip OK: forward then reverse == original")


if __name__ == "__main__":
    main()
