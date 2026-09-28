"""Self-tests for the linepatch library (stdlib unittest)."""
import difflib
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from linepatch import (
    ApplyError, ApplyOptions, FilePatch, Hunk, HunkLine, Patch,
    apply_patch_to_tree, apply_to_text, format_patch, parse_patch,
    reversed_patch, PatchParseError,
)
from linepatch.model import ADD, CONTEXT, REMOVE


def make_patch(old, new, path="f.txt", context=3):
    """Build a unified diff between two lists of lines (no trailing \n)."""
    diff = difflib.unified_diff(old, new, f"a/{path}", f"b/{path}",
                                n=context, lineterm="")
    return "\n".join(diff) + "\n"


class ParseTests(unittest.TestCase):
    def test_multi_file_multi_hunk(self):
        text = (
            "diff --git a/a.txt b/a.txt\n"
            "index 111..222 100644\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " x\n"
            "-old\n"
            "+new\n"
            " y\n"
            "@@ -10,2 +10,2 @@\n"
            "-p\n"
            "+q\n"
            " tail\n"
            "--- a/b.txt\n"
            "+++ b/b.txt\n"
            "@@ -1 +1 @@ section heading\n"
            "-1\n"
            "+2\n"
        )
        p = parse_patch(text)
        self.assertEqual(len(p.files), 2)
        self.assertEqual(p.files[0].old_path, "a.txt")
        self.assertEqual(p.files[0].new_path, "a.txt")
        self.assertEqual(len(p.files[0].hunks), 2)
        self.assertEqual(p.files[1].hunks[0].section, "section heading")
        self.assertEqual(p.files[1].hunks[0].old_count, 1)  # omitted count
        self.assertEqual(p.hunk_count, 3)

    def test_count_mismatch_rejected(self):
        with self.assertRaises(PatchParseError):
            parse_patch("--- a/f\n+++ b/f\n@@ -1,2 +1,1 @@\n-a\n+b\n")

    def test_empty_patch_rejected(self):
        with self.assertRaises(PatchParseError):
            parse_patch("no patches here\n")

    def test_no_eol_marker(self):
        text = ("--- a/f\n+++ b/f\n@@ -1 +1 @@\n"
                "-a\n\\ No newline at end of file\n"
                "+b\n\\ No newline at end of file\n")
        p = parse_patch(text)
        h = p.files[0].hunks[0]
        self.assertTrue(h.old_no_eol)
        self.assertTrue(h.new_no_eol)

    def test_format_roundtrip(self):
        text = ("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n"
                " ctx\n"
                "-a\n\\ No newline at end of file\n"
                "+b\n\\ No newline at end of file\n")
        p = parse_patch(text)
        p2 = parse_patch(format_patch(p))
        self.assertEqual(p, p2)

    def test_body_lines_looking_like_headers_consumed_by_count(self):
        # body lines that happen to start with "--- "/"+++ "/"@@ " must be
        # consumed as hunk body per the header counts, not start a new section
        text = (
            "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n"
            " head\n"
            "---- old header-looking line\n"
            "++++ new header-looking line\n"
            " tail\n"
            "--- a/next\n+++ b/next\n@@ -1 +1 @@\n"
            "-x\n+y\n"
        )
        p = parse_patch(text)
        self.assertEqual(len(p.files), 2)
        h = p.files[0].hunks[0]
        self.assertEqual(
            [(l.kind, l.text) for l in h.lines],
            [(CONTEXT, "head"),
             (REMOVE, "--- old header-looking line"),
             (ADD, "+++ new header-looking line"),
             (CONTEXT, "tail")],
        )
        self.assertEqual(p.files[1].old_path, "next")
        # the first section must also survive a serialise/parse roundtrip
        self.assertEqual(parse_patch(format_patch(p)), p)


class ApplyTests(unittest.TestCase):
    def apply(self, content, patch_text, **kw):
        p = parse_patch(patch_text)
        return apply_to_text(content, p.files[0], **kw)[0]

    def test_basic(self):
        out = self.apply("a\nb\nc\n", make_patch(["a", "b", "c"], ["a", "B", "c"]))
        self.assertEqual(out, "a\nB\nc\n")

    def test_offset_search(self):
        # target has 10 extra lines prepended; hunk must drift to find context
        old = [f"line{i}" for i in range(20)]
        new = old[:10] + ["CHANGED"] + old[11:]
        patch = make_patch(old, new)
        target = "\n".join([f"extra{i}" for i in range(10)] + old) + "\n"
        p = parse_patch(patch)
        out, reports = apply_to_text(target, p.files[0])
        self.assertIn("CHANGED", out)
        self.assertEqual(reports[0].offset, 10)

    def test_offset_limit(self):
        old = [f"line{i}" for i in range(20)]
        new = old[:10] + ["CHANGED"] + old[11:]
        patch = make_patch(old, new)
        target = "\n".join([f"extra{i}" for i in range(10)] + old) + "\n"
        p = parse_patch(patch)
        with self.assertRaises(ApplyError):
            apply_to_text(target, p.files[0], ApplyOptions(max_offset=5))

    def test_conflict_diagnostic(self):
        old = [f"line{i}" for i in range(20)]
        new = old[:10] + ["CHANGED"] + old[11:]
        patch = make_patch(old, new)
        target_lines = list(old)
        target_lines[9] = "someone else edited this"  # break a context line
        target = "\n".join(target_lines) + "\n"
        p = parse_patch(patch)
        with self.assertRaises(ApplyError) as cm:
            apply_to_text(target, p.files[0])
        failure = cm.exception.failures[0]
        self.assertEqual(failure.hunk_index, 0)
        self.assertIsNotNone(failure.best_position)
        self.assertGreater(failure.best_ratio, 0.5)
        self.assertLess(failure.best_ratio, 1.0)
        preview = "\n".join(failure.preview)
        self.assertIn("someone else edited this", preview)
        self.assertIn("line 10", preview)  # 1-based position of the difference

    def test_repeated_context(self):
        # identical blocks: hunk must apply at the position nearest the header
        block = ["same1", "same2", "target", "same3", "same4"]
        old = block + ["gap"] * 5 + block
        new = block + ["gap"] * 5 + block[:2] + ["CHANGED"] + block[3:]
        patch = make_patch(old, new)
        out = self.apply("\n".join(old) + "\n", patch)
        self.assertEqual(out, "\n".join(new) + "\n")

    def test_empty_file_creation(self):
        patch = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
        out = self.apply("", patch)
        self.assertEqual(out, "hello\nworld\n")

    def test_no_trailing_newline(self):
        patch = ("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n"
                 " keep\n"
                 "-old\n\\ No newline at end of file\n"
                 "+new\n\\ No newline at end of file\n")
        out = self.apply("keep\nold", patch)
        self.assertEqual(out, "keep\nnew")  # still no trailing newline

    def test_no_trailing_newline_mismatch(self):
        # patch expects no trailing newline, file has one -> no exact match
        patch = ("--- a/f\n+++ b/f\n@@ -1 +1 @@\n"
                "-old\n\\ No newline at end of file\n"
                "+new\n\\ No newline at end of file\n")
        with self.assertRaises(ApplyError):
            self.apply("old\n", patch)

    def test_pure_insertion_at_eof_without_newline(self):
        # insert after the last line of a file that has no final newline;
        # the missing newline must not push the match one line outward
        patch = "--- a/f\n+++ b/f\n@@ -1,0 +2 @@\n+inserted\n"
        out, reports = apply_to_text("a", parse_patch(patch).files[0])
        self.assertEqual(out, "a\ninserted\n")
        self.assertEqual(reports[0].applied_at, 2)
        self.assertEqual(reports[0].offset, 0)

    def test_crlf_target_lf_patch(self):
        patch = make_patch(["a", "b", "c"], ["a", "B", "c"])
        self.assertNotIn("\r", patch)
        out = self.apply("a\r\nb\r\nc\r\n", patch)
        self.assertEqual(out, "a\r\nB\r\nc\r\n")  # target EOL style preserved

    def test_missing_target_file(self):
        patch = make_patch(["a"], ["b"])
        p = parse_patch(patch)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ApplyError) as cm:
                apply_patch_to_tree(p, root=d)
            self.assertIn("missing", str(cm.exception))

    def test_tree_new_and_delete(self):
        patch = ("--- /dev/null\n+++ b/created.txt\n@@ -0,0 +1 @@\n+hi\n"
                 "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n")
        p = parse_patch(patch)
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "gone.txt"), "w") as f:
                f.write("bye\n")
            apply_patch_to_tree(p, root=d)
            with open(os.path.join(d, "created.txt")) as f:
                self.assertEqual(f.read(), "hi\n")
            self.assertFalse(os.path.exists(os.path.join(d, "gone.txt")))

    def test_multi_hunk_offsets_accumulate(self):
        old = [f"l{i}" for i in range(30)]
        new = ["inserted"] + old[:15] + ["CHANGED"] + old[16:]
        patch = make_patch(old, new)
        out = self.apply("\n".join(old) + "\n", patch)
        self.assertEqual(out, "\n".join(new) + "\n")


class RoundTripTests(unittest.TestCase):
    def test_forward_then_reverse_restores_original(self):
        rng = random.Random(20260927)
        words = ["alpha", "beta", "gamma", "delta", "eps", "zeta", "eta",
                 "theta", "iota", "kappa"]
        for trial in range(300):
            n = rng.randint(0, 60)
            old = [f"{rng.choice(words)}{rng.randint(0, 5)}" for _ in range(n)]
            new = list(old)
            for _ in range(rng.randint(1, 6)):
                op = rng.choice(("ins", "del", "mod"))
                if op == "ins" or not new:
                    pos = rng.randint(0, len(new))
                    new[pos:pos] = [f"added{rng.randint(0, 99)}"]
                elif op == "del":
                    pos = rng.randrange(len(new))
                    del new[pos:pos + rng.randint(1, 3)]
                else:
                    pos = rng.randrange(len(new))
                    new[pos] = f"edited{rng.randint(0, 99)}"
            if old == new:
                continue
            patch_text = make_patch(old, new, path=f"t{trial}.txt")
            patch = parse_patch(patch_text)
            original = "\n".join(old) + ("\n" if old else "")
            modified = "\n".join(new) + ("\n" if new else "")
            fp = patch.files[0]
            # forward
            out, _ = apply_to_text(original, fp)
            self.assertEqual(out, modified, f"trial {trial}")
            # reverse must restore the original exactly
            back, _ = apply_to_text(out, reversed_patch(patch).files[0])
            self.assertEqual(back, original, f"trial {trial} (reverse)")

    def test_reverse_roundtrip_no_trailing_newline(self):
        patch = parse_patch(
            "--- a/f\n+++ b/f\n@@ -1,2 +1,3 @@\n"
            " keep\n"
            "-old\n\\ No newline at end of file\n"
            "+new1\n+new2\n\\ No newline at end of file\n")
        fp = patch.files[0]
        out, _ = apply_to_text("keep\nold", fp)
        self.assertEqual(out, "keep\nnew1\nnew2")
        back, _ = apply_to_text(out, reversed_patch(patch).files[0])
        self.assertEqual(back, "keep\nold")


if __name__ == "__main__":
    unittest.main(verbosity=2)
