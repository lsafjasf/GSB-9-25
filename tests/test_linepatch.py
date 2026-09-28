import difflib
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from linepatch import (
    ApplyConfig,
    HunkConflictError,
    MissingTargetError,
    PatchApplyError,
    PatchParseError,
    apply_file_patch,
    apply_patch,
    parse_patch,
    reverse_patch,
)

BASIC = """\
--- a/src/a.py
+++ b/src/a.py
@@ -1,4 +1,5 @@
 line1
-line2
+line2 changed
 line3
+line3.5
 line4
@@ -10,3 +11,2 @@
 ctx
-gone
 tail
--- a/src/b.py
+++ b/src/b.py
@@ -1,2 +1,2 @@
-x
+y
 z
"""


def make_diff(old_lines, new_lines, n=3, path="f.txt"):
    return "".join(
        difflib.unified_diff(
            old_lines, new_lines,
            fromfile="a/" + path, tofile="b/" + path, n=n,
        )
    )


A_OLD = ("".join("line%d\n" % i for i in range(1, 10))
         + "ctx\ngone\ntail\nline13\nline14\n")


class TestParse(unittest.TestCase):
    def test_multi_file_multi_hunk(self):
        p = parse_patch(BASIC)
        self.assertEqual(len(p.files), 2)
        self.assertEqual(p.files[0].old_path, "a/src/a.py")
        self.assertEqual(p.files[0].new_path, "b/src/a.py")
        self.assertEqual(len(p.files[0].hunks), 2)
        self.assertEqual(len(p.files[1].hunks), 1)
        h = p.files[0].hunks[0]
        self.assertEqual((h.old_start, h.old_count, h.new_start, h.new_count),
                         (1, 4, 1, 5))
        kinds = [l.kind for l in h.lines]
        self.assertEqual(kinds,
                         ["context", "del", "add", "context", "add", "context"])
        self.assertEqual(h.old_lines[1].text, "line2")

    def test_git_preamble_skipped(self):
        text = ("diff --git a/x b/x\nindex 123..456 100644\n"
                "new file mode 100755\n" + BASIC)
        p = parse_patch(text)
        self.assertEqual(len(p.files), 2)

    def test_crlf_patch_text(self):
        p = parse_patch(BASIC.replace("\n", "\r\n"))
        self.assertEqual(p.files[0].hunks[0].lines[0].text, "line1")

    def test_truncated_hunk_rejected(self):
        with self.assertRaises(PatchParseError):
            parse_patch("--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n b\n")

    def test_render_roundtrip(self):
        p = parse_patch(BASIC)
        p2 = parse_patch(p.render())
        self.assertEqual(p.render(), p2.render())


class TestApply(unittest.TestCase):
    def test_basic_apply(self):
        p = parse_patch(BASIC)
        a_new = apply_file_patch(p.files[0], A_OLD)
        self.assertIn("line2 changed\n", a_new)
        self.assertIn("line3.5\n", a_new)
        self.assertNotIn("gone\n", a_new)
        self.assertIn("ctx\ntail\n", a_new)
        b_new = apply_file_patch(p.files[1], "x\nz\n")
        self.assertEqual(b_new, "y\nz\n")

    def test_offset_search_within_limit(self):
        # target has 5 extra lines prepended: hunk must be found at offset +5
        p = parse_patch(BASIC)
        content = "extra\n" * 5 + A_OLD
        out = apply_file_patch(p.files[0], content, ApplyConfig(max_offset=10))
        self.assertIn("line2 changed\n", out)

    def test_offset_search_beyond_limit_fails(self):
        p = parse_patch(BASIC)
        content = "extra\n" * 50 + A_OLD
        with self.assertRaises(HunkConflictError) as cm:
            apply_file_patch(p.files[0], content, ApplyConfig(max_offset=10))
        # unlimited search finds it
        out = apply_file_patch(p.files[0], content, ApplyConfig(max_offset=None))
        self.assertIn("line2 changed\n", out)
        self.assertEqual(cm.exception.candidates[0][0], 50)  # found at line 51

    def test_conflict_diagnostic(self):
        p = parse_patch(BASIC)
        content = A_OLD.replace("line2\n", "line2 tampered\n")
        with self.assertRaises(HunkConflictError) as cm:
            apply_file_patch(p.files[0], content)
        err = cm.exception
        self.assertEqual(err.path, "b/src/a.py")
        self.assertEqual(err.first_diff[0], 1)  # 0-based line 2
        self.assertEqual(err.first_diff[1], "line2")
        self.assertEqual(err.first_diff[2], "line2 tampered")
        msg = str(err)
        self.assertIn("expected at line 1", msg)
        self.assertIn("first difference at line 2", msg)
        self.assertIn("'line2 tampered'", msg)

    def test_conflict_reports_closest_candidate(self):
        # hunk's old-side lines exist elsewhere (shifted), but not at the
        # expected position: the candidate list must point there.
        old = ["alpha\n", "beta\n", "gamma\n", "delta\n", "omega\n"]
        new = ["alpha\n", "beta\n", "GAMMA\n", "delta\n", "omega\n"]
        patch = parse_patch(make_diff(old, new, n=2))
        target = ["junk\n"] * 3 + old  # real content moved down by 3
        cfg = ApplyConfig(max_offset=0)  # forbid offset search -> conflict
        with self.assertRaises(HunkConflictError) as cm:
            apply_file_patch(patch.files[0], "".join(target), cfg)
        err = cm.exception
        self.assertTrue(err.candidates)
        pos, score, total = err.candidates[0]
        self.assertEqual(pos, 3)
        self.assertEqual(score, total)
        self.assertIn("offset +3", str(err))

    def test_repeated_context_lines_deterministic(self):
        # all context lines identical: nearest position to the header wins
        old = ["same\n"] * 10
        new = old[:]
        new[4] = "different\n"
        patch = parse_patch(make_diff(old, new, n=2))
        out = apply_file_patch(patch.files[0], "".join(old))
        self.assertEqual(out, "".join(new))

    def test_empty_file_creation_hunk(self):
        patch = parse_patch("--- /dev/null\n+++ b/new.txt\n"
                            "@@ -0,0 +1,2 @@\n+hello\n+world\n")
        self.assertTrue(patch.files[0].is_creation)
        out = apply_file_patch(patch.files[0], "")
        self.assertEqual(out, "hello\nworld\n")

    def test_no_trailing_newline(self):
        text = ("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n keep\n-old\n"
                "\\ No newline at end of file\n+new\n"
                "\\ No newline at end of file\n")
        p = parse_patch(text)
        self.assertFalse(p.files[0].hunks[0].lines[-1].has_newline)
        out = apply_file_patch(p.files[0], "keep\nold")
        self.assertEqual(out, "keep\nnew")
        # and back
        rev = reverse_patch(p)
        self.assertEqual(apply_file_patch(rev.files[0], out), "keep\nold")

    def test_no_newline_marker_mismatch_is_conflict(self):
        text = ("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n"
                "\\ No newline at end of file\n+new\n")
        p = parse_patch(text)
        with self.assertRaises(HunkConflictError):
            apply_file_patch(p.files[0], "old\n")  # file HAS trailing newline

    def test_crlf_target_lf_patch_preserved(self):
        p = parse_patch(BASIC)
        content = "x\r\nz\r\n"
        out = apply_file_patch(p.files[1], content)
        self.assertEqual(out, "y\r\nz\r\n")

    def test_eol_forced(self):
        p = parse_patch(BASIC)
        out = apply_file_patch(p.files[1], "x\r\nz\r\n", ApplyConfig(eol="lf"))
        self.assertEqual(out, "y\nz\n")

    def test_pure_deletion_hunk(self):
        patch = parse_patch("--- a/f\n+++ b/f\n@@ -1,3 +1 @@\n keep\n-a\n-b\n")
        out = apply_file_patch(patch.files[0], "keep\na\nb\n")
        self.assertEqual(out, "keep\n")


class TestFilesystem(unittest.TestCase):
    def test_missing_target(self):
        p = parse_patch(BASIC)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(MissingTargetError):
                apply_patch(p, root=d)

    def test_create_and_delete_file(self):
        create = parse_patch("--- /dev/null\n+++ b/new.txt\n"
                             "@@ -0,0 +1 @@\n+hello\n")
        delete = parse_patch("--- a/new.txt\n+++ /dev/null\n"
                             "@@ -1 +0,0 @@\n-hello\n")
        with tempfile.TemporaryDirectory() as d:
            actions = apply_patch(create, root=d)
            self.assertEqual(actions, ["created new.txt"])
            with open(os.path.join(d, "new.txt")) as f:
                self.assertEqual(f.read(), "hello\n")
            actions = apply_patch(delete, root=d)
            self.assertEqual(actions, ["deleted new.txt"])
            self.assertFalse(os.path.exists(os.path.join(d, "new.txt")))

    def test_create_disabled(self):
        create = parse_patch("--- /dev/null\n+++ b/new.txt\n"
                             "@@ -0,0 +1 @@\n+hello\n")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(PatchApplyError):
                apply_patch(create, root=d, config=ApplyConfig(allow_create=False))

    def test_apply_patch_to_disk(self):
        p = parse_patch(BASIC)
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "src"))
            with open(os.path.join(d, "src", "a.py"), "w") as f:
                f.write(A_OLD)
            with open(os.path.join(d, "src", "b.py"), "w") as f:
                f.write("x\nz\n")
            apply_patch(p, root=d)
            with open(os.path.join(d, "src", "a.py")) as f:
                self.assertIn("line2 changed\n", f.read())


class TestRoundtrip(unittest.TestCase):
    def test_random_forward_reverse(self):
        rng = random.Random(20260928)
        for trial in range(200):
            n = rng.randrange(0, 120)
            old = ["line-%d-%d\n" % (trial, i) for i in range(n)]
            new = list(old)
            # random edits: delete / insert / modify chunks
            for _ in range(rng.randrange(1, 8)):
                op = rng.choice(["del", "ins", "mod"])
                if not new and op != "ins":
                    continue
                i = rng.randrange(0, len(new) + (1 if op == "ins" else 0))
                if op == "del":
                    del new[i:min(len(new), i + rng.randrange(1, 4))]
                elif op == "ins":
                    new[i:i] = ["added-%d-%d\n" % (trial, k)
                                for k in range(rng.randrange(1, 4))]
                else:
                    new[i] = "modified-%d-%d\n" % (trial, i)
            ctx = rng.choice([0, 1, 3])
            text = make_diff(old, new, n=ctx)
            patch = parse_patch(text)
            old_s, new_s = "".join(old), "".join(new)
            if not patch.files:  # no changes
                self.assertEqual(old_s, new_s)
                continue
            fp = patch.files[0]
            applied = apply_file_patch(fp, old_s)
            self.assertEqual(applied, new_s,
                             "forward apply mismatch, trial %d" % trial)
            rev = reverse_patch(patch)
            restored = apply_file_patch(rev.files[0], applied)
            self.assertEqual(restored, old_s,
                             "reverse apply mismatch, trial %d" % trial)

    def test_random_roundtrip_with_drift(self):
        # target drifted by inserted lines; offset search must still give a
        # perfect forward/reverse roundtrip
        rng = random.Random(7)
        old = ["l%03d\n" % i for i in range(300)]
        new = list(old)
        for i in range(0, 300, 30):
            new[i] = "changed-%d\n" % i
        patch = parse_patch(make_diff(old, new, n=3))
        drifted = ["drift\n"] * 17 + old[:100] + ["more drift\n"] + old[100:]
        cfg = ApplyConfig(max_offset=50)
        applied = apply_file_patch(patch.files[0], "".join(drifted), cfg)
        restored = apply_file_patch(reverse_patch(patch).files[0], applied, cfg)
        self.assertEqual(restored, "".join(drifted))


if __name__ == "__main__":
    unittest.main(verbosity=2)
