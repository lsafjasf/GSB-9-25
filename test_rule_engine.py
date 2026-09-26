"""Self-tests for rule_engine (standard-library unittest only).

Covers: empty rule set, all rules matched, condition evaluation errors,
missing fields, actions overwriting each other (conflicts), dynamic rule
add/remove, determinism of traces, and side-effect assertions proving
that non-matched rules never touch the state.
"""

import unittest

from rule_engine import (
    And,
    ConditionError,
    ConflictError,
    Engine,
    Field,
    Flag,
    Not,
    Or,
    Rule,
    Set,
)


def make_engine(strategy="priority"):
    return Engine(conflict_strategy=strategy)


class TestBasics(unittest.TestCase):
    def test_empty_rule_set(self):
        engine = make_engine()
        facts = {"age": 30}
        result = engine.run(facts)
        self.assertEqual(result.state.values, facts)
        self.assertEqual(result.state.flags, set())
        self.assertEqual(result.trace.entries, [])
        self.assertEqual(result.trace.conflicts, [])

    def test_all_rules_matched(self):
        engine = make_engine()
        engine.add_rule(Rule("adult", Field("age").ge(18),
                             [Set("category", "adult")], priority=1))
        engine.add_rule(Rule("vip", Field("tier").in_(["gold", "platinum"]),
                             [Flag("vip"), Set("discount", 0.2)]))
        result = engine.run({"age": 30, "tier": "gold"})
        self.assertEqual(
            [e.status for e in result.trace.entries], ["matched", "matched"]
        )
        self.assertEqual(result.state.values["category"], "adult")
        self.assertEqual(result.state.values["discount"], 0.2)
        self.assertIn("vip", result.state.flags)

    def test_logic_combinations(self):
        cond = And(Field("age").ge(18), Or(Field("tier").eq("gold"),
                                           Field("score").gt(900)))
        engine = make_engine()
        engine.add_rule(Rule("combo", cond, [Flag("ok")]))
        self.assertIn("ok", engine.run({"age": 20, "tier": "x", "score": 950}).state.flags)
        self.assertNotIn("ok", engine.run({"age": 20, "tier": "x", "score": 100}).state.flags)
        self.assertNotIn("ok", engine.run({"age": 10, "tier": "gold", "score": 100}).state.flags)

    def test_not_and_membership(self):
        engine = make_engine()
        engine.add_rule(Rule("not-banned", Not(Field("status").in_(["banned", "locked"])),
                             [Flag("allowed")]))
        self.assertIn("allowed", engine.run({"status": "active"}).state.flags)
        self.assertNotIn("allowed", engine.run({"status": "banned"}).state.flags)


class TestErrorsAndMissingFields(unittest.TestCase):
    def test_missing_field_skips_rule_without_side_effects(self):
        engine = make_engine()
        engine.add_rule(Rule("needs-age", Field("age").ge(18),
                             [Set("category", "adult"), Flag("adult")]))
        facts = {"name": "bob"}
        result = engine.run(facts)
        entry = result.trace.entries[0]
        self.assertEqual(entry.status, "skipped")
        self.assertIn("condition error", entry.reason)
        self.assertIn("missing", entry.reason)
        # side-effect assertion: state identical to input
        self.assertEqual(result.state.values, facts)
        self.assertEqual(result.state.flags, set())

    def test_condition_eval_error_type_mismatch(self):
        engine = make_engine()
        engine.add_rule(Rule("bad-compare", Field("name").gt(10),
                             [Flag("never")]))
        facts = {"name": "alice"}
        result = engine.run(facts)
        entry = result.trace.entries[0]
        self.assertEqual(entry.status, "skipped")
        self.assertIn("condition error", entry.reason)
        self.assertEqual(result.state.values, facts)
        self.assertEqual(result.state.flags, set())

    def test_error_in_one_rule_does_not_stop_others(self):
        engine = make_engine()
        engine.add_rule(Rule("broken", Field("missing").eq(1), [Flag("x")], priority=5))
        engine.add_rule(Rule("fine", Field("a").eq(1), [Flag("y")], priority=1))
        result = engine.run({"a": 1})
        statuses = {e.rule: e.status for e in result.trace.entries}
        self.assertEqual(statuses, {"broken": "skipped", "fine": "matched"})
        self.assertIn("y", result.state.flags)


class TestConflicts(unittest.TestCase):
    def _conflicting_engine(self, strategy):
        engine = make_engine(strategy)
        engine.add_rule(Rule("low", Field("x").eq(1),
                             [Set("verdict", "LOW")], priority=1))
        engine.add_rule(Rule("high", Field("x").eq(1),
                             [Set("verdict", "HIGH")], priority=10))
        return engine

    def test_priority_strategy_keeps_higher_priority(self):
        result = self._conflicting_engine("priority").run({"x": 1})
        self.assertEqual(result.state.values["verdict"], "HIGH")
        self.assertEqual(len(result.trace.conflicts), 1)
        conflict = result.trace.conflicts[0]
        self.assertEqual(conflict.field, "verdict")
        self.assertEqual(conflict.existing_rule, "high")
        self.assertEqual(conflict.attempted_rule, "low")
        self.assertIn("conflict", result.trace_text())

    def test_declaration_order_breaks_priority_ties(self):
        engine = make_engine()
        engine.add_rule(Rule("first", Field("x").eq(1), [Set("v", "first")]))
        engine.add_rule(Rule("second", Field("x").eq(1), [Set("v", "second")]))
        result = engine.run({"x": 1})
        self.assertEqual(result.state.values["v"], "first")
        self.assertEqual(result.trace.conflicts[0].attempted_rule, "second")

    def test_reject_strategy_raises(self):
        engine = self._conflicting_engine("reject")
        with self.assertRaises(ConflictError) as ctx:
            engine.run({"x": 1})
        self.assertEqual(ctx.exception.conflicts[0].field, "verdict")

    def test_same_value_is_not_a_conflict(self):
        engine = make_engine()
        engine.add_rule(Rule("a", Field("x").eq(1), [Set("v", "same")], priority=2))
        engine.add_rule(Rule("b", Field("x").eq(1), [Set("v", "same")], priority=1))
        result = engine.run({"x": 1})
        self.assertEqual(result.trace.conflicts, [])
        self.assertEqual(result.state.values["v"], "same")

    def test_actions_overwrite_same_field_within_one_rule(self):
        engine = make_engine()
        engine.add_rule(Rule("multi", Field("x").eq(1),
                             [Set("v", 1), Set("v", 2)]))
        result = engine.run({"x": 1})
        self.assertEqual(result.state.values["v"], 2)
        changes = result.trace.entries[0].changes
        self.assertEqual(len(changes), 2)


class TestDynamicRulesAndDeterminism(unittest.TestCase):
    def test_add_and_remove_rules(self):
        engine = make_engine()
        rid = engine.add_rule(Rule("temp", Field("a").eq(1), [Flag("t")]))
        self.assertIn("t", engine.run({"a": 1}).state.flags)
        self.assertTrue(engine.remove_rule(rid))
        self.assertFalse(engine.remove_rule(rid))
        self.assertNotIn("t", engine.run({"a": 1}).state.flags)

    def test_added_rule_participates_immediately(self):
        engine = make_engine()
        self.assertEqual(engine.run({"a": 1}).state.flags, set())
        engine.add_rule(Rule("late", Field("a").eq(1), [Flag("late")]))
        self.assertIn("late", engine.run({"a": 1}).state.flags)

    def test_trace_is_deterministic(self):
        engine = make_engine()
        engine.add_rule(Rule("r1", Field("a").ge(1), [Set("v", "r1")], priority=3))
        engine.add_rule(Rule("r2", Field("a").ge(1), [Set("v", "r2")], priority=3))
        engine.add_rule(Rule("r3", Field("b").eq(2), [Flag("b")], priority=1))
        facts = {"a": 5, "b": 9}
        first = engine.run(facts)
        for _ in range(5):
            again = engine.run(facts)
            self.assertEqual(again.trace_text(), first.trace_text())
            self.assertEqual(again.state.values, first.state.values)
            self.assertEqual(again.state.flags, first.state.flags)


class TestSideEffects(unittest.TestCase):
    def test_unmatched_rules_produce_no_side_effects(self):
        engine = make_engine()
        engine.add_rule(Rule("hit", Field("a").eq(1), [Set("hit", True)]))
        engine.add_rule(Rule("miss", Field("a").eq(999),
                             [Set("miss", True), Flag("missed")]))
        facts = {"a": 1}
        before = dict(facts)
        result = engine.run(facts)
        # input facts must not be mutated
        self.assertEqual(facts, before)
        # only the matched rule's changes exist
        self.assertEqual(result.state.values, {"a": 1, "hit": True})
        self.assertNotIn("miss", result.state.values)
        self.assertEqual(result.state.flags, set())
        # trace records exactly one change, from the matched rule
        changes = [c for e in result.trace.entries for c in e.changes]
        self.assertEqual(len(changes), 1)

    def test_condition_error_rule_produces_no_side_effects(self):
        engine = make_engine()
        engine.add_rule(Rule("err", Field("nope").gt(1),
                             [Set("touched", True), Flag("touched")]))
        result = engine.run({"ok": 1})
        self.assertEqual(result.state.values, {"ok": 1})
        self.assertEqual(result.state.flags, set())


class TestTraceContent(unittest.TestCase):
    def test_trace_records_changes_and_reasons(self):
        engine = make_engine()
        engine.add_rule(Rule("set-tier", Field("age").ge(18),
                             [Set("tier", "adult"), Flag("reviewed")]))
        engine.add_rule(Rule("skip-me", Field("age").lt(5), [Flag("baby")]))
        text = engine.run({"age": 40}).trace_text()
        self.assertIn("[matched] set-tier", text)
        self.assertIn("set tier: '<missing>' -> 'adult'", text)
        self.assertIn("flag reviewed: added", text)
        self.assertIn("[skipped] skip-me", text)
        self.assertIn("condition not satisfied", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
