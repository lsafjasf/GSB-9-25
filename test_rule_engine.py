"""Self-tests for rule_engine. Run: python3 -m unittest test_rule_engine -v"""

import json
import time
import unittest

from rule_engine import (
    ConflictError,
    Rule,
    RuleEngine,
    evaluate_condition,
)


def make_engine(strategy="priority"):
    return RuleEngine(conflict_strategy=strategy)


class TestConditionDSL(unittest.TestCase):
    def test_comparison_operators(self):
        facts = {"age": 20, "name": "ada"}
        self.assertTrue(evaluate_condition(
            {"field": "age", "op": ">=", "value": 18}, facts))
        self.assertFalse(evaluate_condition(
            {"field": "age", "op": "<", "value": 18}, facts))
        self.assertTrue(evaluate_condition(
            {"field": "name", "op": "==", "value": "ada"}, facts))
        self.assertTrue(evaluate_condition(
            {"field": "name", "op": "!=", "value": "bob"}, facts))

    def test_membership(self):
        facts = {"tier": "gold"}
        self.assertTrue(evaluate_condition(
            {"field": "tier", "op": "in", "value": ["gold", "silver"]}, facts))
        self.assertTrue(evaluate_condition(
            {"field": "tier", "op": "not_in", "value": ["basic"]}, facts))
        self.assertFalse(evaluate_condition(
            {"field": "tier", "op": "in", "value": ["basic"]}, facts))

    def test_logic_combination(self):
        facts = {"age": 20, "tier": "gold", "region": "eu"}
        cond = {"and": [
            {"field": "age", "op": ">=", "value": 18},
            {"or": [
                {"field": "tier", "op": "in", "value": ["gold"]},
                {"field": "region", "op": "==", "value": "us"},
            ]},
            {"not": {"field": "region", "op": "==", "value": "cn"}},
        ]}
        self.assertTrue(evaluate_condition(cond, facts))
        facts["region"] = "cn"
        self.assertFalse(evaluate_condition(cond, facts))


class TestEmptyAndAllMatch(unittest.TestCase):
    def test_empty_rule_set(self):
        engine = make_engine()
        facts = {"a": 1}
        result = engine.run(facts)
        self.assertEqual(result.state, {"a": 1})
        self.assertEqual(result.flags, set())
        self.assertEqual(result.trace["matched"], [])
        self.assertEqual(result.trace["evaluations"], [])
        self.assertEqual(result.trace["actions"], [])
        self.assertEqual(facts, {"a": 1})  # input untouched

    def test_all_rules_match_and_execute_in_order(self):
        engine = make_engine()
        # Declaration order is r1, r2, r3; execution must be priority desc,
        # then declaration order asc: r2 (p=10), r3 (p=5), r1 (p=0).
        engine.add_rule(Rule("r1", {"field": "x", "op": ">", "value": 0},
                             [{"set": "log", "value": "r1"}], priority=0))
        engine.add_rule(Rule("r2", {"field": "x", "op": ">", "value": 0},
                             [{"set": "log2", "value": "r2"}], priority=10))
        engine.add_rule(Rule("r3", {"field": "x", "op": ">", "value": 0},
                             [{"flag": "hit"}], priority=5))
        result = engine.run({"x": 1})
        self.assertEqual(result.trace["matched"], ["r2", "r3", "r1"])
        self.assertEqual(result.state["log"], "r1")
        self.assertEqual(result.state["log2"], "r2")
        self.assertEqual(result.flags, {"hit"})
        applied = [a for a in result.trace["actions"] if a.get("applied", True)]
        self.assertEqual(len(applied), 3)


class TestSkippedRulesHaveNoSideEffects(unittest.TestCase):
    def _engine_with_skips(self):
        engine = make_engine()
        engine.add_rule(Rule(
            "false_cond",
            {"field": "score", "op": ">", "value": 100},
            [{"set": "level", "value": "high"}, {"flag": "promoted"}]))
        engine.add_rule(Rule(
            "missing_field",
            {"field": "nonexistent", "op": "==", "value": 1},
            [{"set": "level", "value": "ghost"}, {"flag": "ghost"}]))
        engine.add_rule(Rule(
            "bad_condition",
            {"field": "score", "op": "???", "value": 1},
            [{"set": "level", "value": "broken"}, {"flag": "broken"}]))
        engine.add_rule(Rule(
            "type_error",
            {"field": "name", "op": "<", "value": 5},
            [{"set": "level", "value": "weird"}]))
        return engine

    def test_skipped_rules_leave_state_untouched(self):
        engine = self._engine_with_skips()
        facts = {"score": 50, "name": "ada"}
        before = dict(facts)
        result = engine.run(facts)
        # Nothing matched -> state must equal the input facts exactly.
        self.assertEqual(result.state, before)
        self.assertEqual(result.flags, set())
        self.assertEqual(result.trace["actions"], [])
        self.assertEqual(result.trace["matched"], [])
        self.assertEqual(facts, before)  # input dict not mutated

    def test_skip_reasons_are_recorded(self):
        engine = self._engine_with_skips()
        result = engine.run({"score": 50, "name": "ada"})
        reasons = {e["rule_id"]: e["reason"]
                   for e in result.trace["evaluations"]}
        self.assertEqual(reasons["false_cond"], "condition_false")
        self.assertTrue(reasons["missing_field"].startswith("field_missing:"))
        self.assertTrue(reasons["bad_condition"].startswith("condition_error:"))
        self.assertTrue(reasons["type_error"].startswith("condition_error:"))
        for entry in result.trace["evaluations"]:
            self.assertEqual(entry["result"], "skipped")


class TestConflictResolution(unittest.TestCase):
    def _conflicting_engine(self, strategy):
        engine = make_engine(strategy)
        engine.add_rule(Rule(
            "low_prio", {"field": "x", "op": "==", "value": 1},
            [{"set": "verdict", "value": "low"}], priority=1))
        engine.add_rule(Rule(
            "high_prio", {"field": "x", "op": "==", "value": 1},
            [{"set": "verdict", "value": "high"}], priority=9))
        return engine

    def test_priority_strategy_keeps_higher_priority_value(self):
        engine = self._conflicting_engine("priority")
        result = engine.run({"x": 1})
        self.assertEqual(result.state["verdict"], "high")
        self.assertEqual(len(result.trace["conflicts"]), 1)
        conflict = result.trace["conflicts"][0]
        self.assertEqual(conflict["field"], "verdict")
        self.assertEqual(conflict["strategy"], "priority")
        self.assertEqual(conflict["kept"],
                         {"rule_id": "high_prio", "value": "high"})
        self.assertEqual(conflict["rejected"],
                         {"rule_id": "low_prio", "value": "low"})
        rejected = [a for a in result.trace["actions"] if not a.get("applied", True)]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["rule_id"], "low_prio")

    def test_declaration_order_breaks_priority_ties(self):
        engine = make_engine("priority")
        engine.add_rule(Rule("first", {"field": "x", "op": "==", "value": 1},
                             [{"set": "v", "value": "A"}], priority=5))
        engine.add_rule(Rule("second", {"field": "x", "op": "==", "value": 1},
                             [{"set": "v", "value": "B"}], priority=5))
        result = engine.run({"x": 1})
        self.assertEqual(result.state["v"], "A")
        self.assertEqual(result.trace["conflicts"][0]["kept"]["rule_id"], "first")

    def test_reject_strategy_raises_and_records(self):
        engine = self._conflicting_engine("reject")
        with self.assertRaises(ConflictError) as ctx:
            engine.run({"x": 1})
        err = ctx.exception
        self.assertEqual(len(err.conflicts), 1)
        self.assertEqual(err.conflicts[0]["field"], "verdict")
        # Trace is available on the exception and shows the rejected action.
        rejected = [a for a in err.trace["actions"] if not a.get("applied", True)]
        self.assertEqual(rejected[0]["rule_id"], "low_prio")

    def test_same_value_is_not_a_conflict(self):
        engine = make_engine("priority")
        engine.add_rule(Rule("a", {"field": "x", "op": "==", "value": 1},
                             [{"set": "v", "value": "same"}], priority=2))
        engine.add_rule(Rule("b", {"field": "x", "op": "==", "value": 1},
                             [{"set": "v", "value": "same"}], priority=1))
        result = engine.run({"x": 1})
        self.assertEqual(result.trace["conflicts"], [])
        self.assertEqual(result.state["v"], "same")

    def test_action_override_within_one_rule_is_last_wins(self):
        engine = make_engine("priority")
        engine.add_rule(Rule("r", {"field": "x", "op": "==", "value": 1},
                             [{"set": "v", "value": "first"},
                              {"set": "v", "value": "second"}]))
        result = engine.run({"x": 1})
        self.assertEqual(result.state["v"], "second")
        self.assertEqual(result.trace["conflicts"], [])


class TestDynamicRulesAndDeterminism(unittest.TestCase):
    def test_add_and_remove_rules(self):
        engine = make_engine()
        rule = Rule("temp", {"field": "x", "op": "==", "value": 1},
                    [{"flag": "temp_hit"}])
        engine.add_rule(rule)
        self.assertEqual(engine.run({"x": 1}).flags, {"temp_hit"})
        self.assertTrue(engine.remove_rule("temp"))
        self.assertFalse(engine.remove_rule("temp"))
        self.assertEqual(engine.run({"x": 1}).flags, set())
        with self.assertRaises(ValueError):
            engine.add_rule(Rule("temp", {"field": "x", "op": "==", "value": 1},
                                 [{"flag": "f"}]))
            engine.add_rule(Rule("temp", {"field": "x", "op": "==", "value": 1},
                                 [{"flag": "f"}]))

    def test_trace_is_deterministic_across_runs(self):
        engine = make_engine()
        engine.add_rule(Rule("r1", {"field": "age", "op": ">=", "value": 18},
                             [{"set": "adult", "value": True}], priority=3))
        engine.add_rule(Rule("r2", {"field": "tier", "op": "in", "value": ["gold"]},
                             [{"flag": "vip"}, {"set": "adult", "value": False}],
                             priority=5))
        engine.add_rule(Rule("r3", {"field": "ghost", "op": "==", "value": 1},
                             [{"set": "x", "value": 1}]))
        facts = {"age": 30, "tier": "gold"}
        first = engine.run(facts)
        second = engine.run(facts)
        self.assertEqual(json.dumps(first.trace, sort_keys=True),
                         json.dumps(second.trace, sort_keys=True))
        self.assertEqual(first.state, second.state)
        self.assertEqual(first.flags, second.flags)


class TestPerformance(unittest.TestCase):
    def test_two_thousand_rules(self):
        engine = make_engine()
        for i in range(2000):
            if i % 2 == 0:
                cond = {"and": [{"field": "n", "op": ">=", "value": i},
                                {"field": "kind", "op": "in", "value": ["a", "b"]}]}
            else:
                cond = {"field": "n", "op": "<", "value": i}
            engine.add_rule(Rule("rule-%04d" % i, cond,
                                 [{"set": "bucket_%d" % (i % 50), "value": i},
                                  {"flag": "f%d" % i}],
                                 priority=i % 7))
        facts = {"n": 1000, "kind": "a"}
        start = time.perf_counter()
        result = engine.run(facts)
        elapsed = time.perf_counter() - start
        print("\n[perf] 2000 rules, one run: %.2f ms (%d matched, %d actions)"
              % (elapsed * 1000, len(result.trace["matched"]),
                 len(result.trace["actions"])))
        self.assertLess(elapsed, 2.0)
        self.assertGreater(len(result.trace["matched"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
