"""Reproduction tests: each test stably reproduces one production bug class.

They pin the defects of ``BuggyMemoryIndex`` so that anyone can confirm the
problems existed before the fix; the fixed ``MemoryIndex`` satisfies the same
scenarios without violations (see test_memory_index.py).
"""

import unittest

import conformance as c


buggy = c.FACTORIES["buggy"]


class ReproDefects(unittest.TestCase):
    def test_delete_current_drops_next_record(self):
        # Bug class 1: deleting the record under the cursor skips the record
        # that shifts into it.
        violations = c.scenario_delete_current(buggy)
        self.assertTrue(violations)
        self.assertTrue(
            any("missing snapshot records" in v or "visit count" in v
                for v in violations),
            violations,
        )

    def test_delete_unvisited_record_is_lost(self):
        # Same skip mechanism, exercised from the "delete ahead of cursor"
        # direction required by the task.
        violations = c.scenario_delete_unvisited(buggy)
        self.assertTrue(violations)

    def test_delete_then_reinsert_same_key_visits_twice(self):
        # Bug class 2: the same live key is delivered twice in one traversal.
        violations = c.scenario_delete_and_reinsert(buggy)
        self.assertTrue(
            any("duplicate visits" in v or "visited twice" in v
                for v in violations),
            violations,
        )

    def test_old_iterator_reads_released_data_after_compact(self):
        # Bug class 3: iterator touches buffers the compactor freed/rewrote.
        violations = c.scenario_compact_during_iteration(buggy)
        self.assertTrue(
            any("iterator error" in v for v in violations),
            violations,
        )

    def test_concurrent_interleaved_insert_and_delete(self):
        # Mixed interleaving combines all three traversal bugs.
        violations = c.scenario_concurrent_interleaving(buggy)
        self.assertTrue(violations)

    def test_stats_drift_from_reality(self):
        # Bug class 4: entries/deleted/capacity disagree with the table.
        violations = c.scenario_stats_consistency(buggy)
        self.assertTrue(violations)


if __name__ == "__main__":
    unittest.main(verbosity=2)
