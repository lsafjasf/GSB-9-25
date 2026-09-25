"""Reproduction tests for the four production issues of BuggyMemoryIndex.

Each test asserts the *defective* behaviour so the bug is pinned down and
cannot silently disappear. The same scenarios appear in
tests/test_memory_index.py as regression tests against the fixed index.
"""

import unittest

from memory_index_buggy import BuggyMemoryIndex


def _filled(keys):
    idx = BuggyMemoryIndex()
    for k in keys:
        idx.put(k, k.upper())
    return idx


class Bug1SkipAfterDeleteCurrentTests(unittest.TestCase):
    def test_delete_current_skips_next(self):
        idx = _filled(["a", "b", "c", "d"])
        it = iter(idx)
        first, _ = next(it)          # 'a'
        idx.delete(first)            # physical pop shifts b,c,d down
        rest = [k for k, _ in it]
        # BUG: 'b' is skipped because the cursor index now points past it.
        self.assertEqual(rest, ["c", "d"])
        self.assertNotIn("b", rest)


class Bug2DuplicateAfterReinsertTests(unittest.TestCase):
    def test_delete_then_reinsert_same_key_visits_twice(self):
        idx = _filled(["a", "b", "c"])
        it = iter(idx)
        first, _ = next(it)          # 'a'
        idx.delete(first)
        idx.put(first, "A2")         # appended at the end of the array
        visited = [first] + [k for k, _ in it]
        # BUG: 'a' is visited twice.
        self.assertEqual(visited.count("a"), 2)


class Bug3StaleIteratorAfterCleanupTests(unittest.TestCase):
    def test_old_iterator_reads_released_data_after_cleanup(self):
        idx = _filled(["a", "b", "c", "d"])
        it = iter(idx)
        next(it)                     # consume 'a'
        idx.cleanup()                # reallocates the slot array
        idx.delete("c")              # only affects the NEW array
        rest = [k for k, _ in it]
        # BUG: the old iterator still walks the detached array and yields
        # 'c', a record that has already been deleted/released.
        self.assertIn("c", rest)


class Bug4SizeDriftTests(unittest.TestCase):
    def test_size_inconsistent_during_traversal(self):
        idx = _filled(["a", "b", "c"])
        it = iter(idx)
        next(it)
        idx.delete("a")
        actual = sum(1 for _ in idx)  # physically 2 records left
        # BUG: len()/stats() still report 3.
        self.assertNotEqual(len(idx), actual)
        self.assertEqual(idx.stats()["size"], 3)
        self.assertEqual(actual, 2)


if __name__ == "__main__":
    unittest.main()
