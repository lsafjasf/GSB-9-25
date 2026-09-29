"""Self-tests for rule_engine. Run: python3 -m unittest test_rule_engine -v"""

import json
import time
import unittest

from rule_engine import (
    ConditionError,
    ConflictError,
    FieldMissingError,
    ReplayMismatchError,
    Rule,
    RuleEngine,
    evaluate_condition,
    format_trace,
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


class TestExplainableConditionTree(unittest.TestCase):
    def test_per_item_results_for_nested_condition(self):
        engine = make_engine()
        engine.add_rule(Rule(
            "vip",
            {"and": [
                {"field": "tier", "op": "in", "value": ["gold", "platinum"]},
                {"or": [{"field": "age", "op": ">=", "value": 18},
                        {"field": "guardian", "op": "==", "value": True}]},
                {"not": {"field": "banned", "op": "==", "value": True}},
            ]},
            [{"flag": "vip"}]))
        facts = {"tier": "gold", "age": 20, "guardian": False, "banned": False}
        entry = engine.run(facts).trace["evaluations"][0]
        self.assertEqual(entry["result"], "matched")
        root = entry["condition"]
        self.assertEqual(root["type"], "and")
        self.assertIs(root["result"], True)
        leaf = root["children"][0]
        self.assertEqual(leaf["type"], "cmp")
        self.assertEqual(leaf["field"], "tier")
        self.assertEqual(leaf["actual"], "gold")
        self.assertIs(leaf["result"], True)
        or_node = root["children"][1]
        self.assertEqual(or_node["type"], "or")
        self.assertIs(or_node["result"], True)
        # "or" short-circuits after age >= 18 is True: guardian unevaluated.
        self.assertIs(or_node["children"][0]["result"], True)
        guardian = or_node["children"][1]
        self.assertEqual(guardian["type"], "unevaluated")
        self.assertEqual(guardian["reason"], "short_circuit")
        not_node = root["children"][2]
        self.assertEqual(not_node["type"], "not")
        self.assertIs(not_node["result"], True)
        self.assertIs(not_node["child"]["result"], False)

    def test_and_short_circuits_on_first_false(self):
        engine = make_engine()
        engine.add_rule(Rule(
            "r",
            {"and": [{"field": "a", "op": "==", "value": 1},
                     {"field": "b", "op": "==", "value": 2}]},
            [{"flag": "f"}]))
        entry = engine.run({"a": 0, "b": 2}).trace["evaluations"][0]
        self.assertEqual(entry["result"], "skipped")
        self.assertEqual(entry["reason"], "condition_false")
        children = entry["condition"]["children"]
        self.assertIs(children[0]["result"], False)
        self.assertEqual(children[1]["type"], "unevaluated")

    def test_error_nodes_keep_original_skip_reasons(self):
        engine = make_engine()
        engine.add_rule(Rule("missing", {"field": "ghost", "op": "==", "value": 1},
                             [{"flag": "f"}]))
        engine.add_rule(Rule("bad_op", {"field": "x", "op": "???", "value": 1},
                             [{"flag": "f"}]))
        engine.add_rule(Rule("bad_type", {"field": "name", "op": "<", "value": 5},
                             [{"flag": "f"}]))
        result = engine.run({"x": 1, "name": "ada"})
        entries = {e["rule_id"]: e for e in result.trace["evaluations"]}
        self.assertEqual(entries["missing"]["reason"], "field_missing: ghost")
        self.assertEqual(entries["missing"]["condition"]["error_type"],
                         "field_missing")
        self.assertTrue(entries["bad_op"]["reason"].startswith("condition_error:"))
        self.assertEqual(entries["bad_op"]["condition"]["error_type"],
                         "condition_error")
        self.assertEqual(entries["bad_type"]["condition"]["error_type"],
                         "condition_error")
        # evaluate_condition still raises the same exception types.
        with self.assertRaises(FieldMissingError):
            evaluate_condition({"field": "ghost", "op": "==", "value": 1}, {})
        with self.assertRaises(ConditionError):
            evaluate_condition({"field": "x", "op": "?", "value": 1}, {"x": 1})


class TestActionCoverageAndFieldSources(unittest.TestCase):
    def test_conflict_rejected_action_marks_overridden_by(self):
        engine = make_engine("priority")
        engine.add_rule(Rule("low", {"field": "x", "op": "==", "value": 1},
                             [{"set": "v", "value": "low"}], priority=1))
        engine.add_rule(Rule("high", {"field": "x", "op": "==", "value": 1},
                             [{"set": "v", "value": "high"}], priority=9))
        result = engine.run({"x": 1})
        rejected = [a for a in result.trace["actions"]
                    if not a.get("applied", True)]
        self.assertEqual(rejected[0]["overridden_by"], "high")
        self.assertEqual(result.trace["field_sources"]["v"],
                         {"source": "high", "action_index": 0})

    def test_within_rule_override_marks_superseded(self):
        engine = make_engine()
        engine.add_rule(Rule("r", {"field": "x", "op": "==", "value": 1},
                             [{"set": "v", "value": "first"},
                              {"set": "v", "value": "second"}]))
        result = engine.run({"x": 1})
        first, second = result.trace["actions"]
        self.assertEqual(first["superseded_by"], "r")
        self.assertNotIn("superseded_by", second)
        self.assertEqual(result.trace["field_sources"]["v"]["source"], "r")

    def test_field_sources_distinguish_input_and_rules(self):
        engine = make_engine()
        engine.add_rule(Rule("r", {"field": "x", "op": ">", "value": 0},
                             [{"set": "y", "value": 10}, {"flag": "hit"}]))
        result = engine.run({"x": 1, "untouched": "keep"})
        sources = result.trace["field_sources"]
        self.assertEqual(sources["untouched"], {"source": "input"})
        self.assertEqual(sources["x"], {"source": "input"})
        self.assertEqual(sources["y"]["source"], "r")
        # Flags are not fields; they stay out of field_sources.
        self.assertNotIn("hit", sources)

    def test_conflict_basis_explains_priority_and_tie(self):
        engine = make_engine("priority")
        engine.add_rule(Rule("low", {"field": "x", "op": "==", "value": 1},
                             [{"set": "v", "value": "L"}], priority=1))
        engine.add_rule(Rule("high", {"field": "x", "op": "==", "value": 1},
                             [{"set": "v", "value": "H"}], priority=9))
        basis = engine.run({"x": 1}).trace["conflicts"][0]["basis"]
        self.assertIn("priority 9 > 1", basis)
        self.assertIn("high", basis)

        tie = make_engine("priority")
        tie.add_rule(Rule("first", {"field": "x", "op": "==", "value": 1},
                          [{"set": "v", "value": "A"}], priority=5))
        tie.add_rule(Rule("second", {"field": "x", "op": "==", "value": 1},
                           [{"set": "v", "value": "B"}], priority=5))
        basis = tie.run({"x": 1}).trace["conflicts"][0]["basis"]
        self.assertIn("priority tie", basis)
        self.assertIn("order 0 < 1", basis)


class TestReplay(unittest.TestCase):
    def _engine(self, strategy="priority"):
        engine = make_engine(strategy)
        engine.add_rule(Rule("adult", {"field": "age", "op": ">=", "value": 18},
                             [{"set": "category", "value": "adult"}], priority=1))
        engine.add_rule(Rule(
            "vip",
            {"and": [{"field": "tier", "op": "in", "value": ["gold"]},
                     {"field": "active", "op": "==", "value": True}]},
            [{"set": "discount", "value": 0.2}, {"flag": "vip"}], priority=10))
        engine.add_rule(Rule(
            "student", {"field": "age", "op": "<", "value": 25},
            [{"set": "discount", "value": 0.1}], priority=5))
        return engine

    def test_replay_reproduces_same_decision(self):
        engine = self._engine()
        facts = {"age": 20, "tier": "gold", "active": True}
        trace = engine.run(facts).trace
        replayed = engine.replay(facts, trace)
        self.assertEqual(replayed.state, trace["final_state"])
        self.assertEqual(replayed.trace, trace)

    def test_replay_detects_tampered_trace(self):
        engine = self._engine()
        facts = {"age": 20, "tier": "gold", "active": True}
        trace = engine.run(facts).trace
        tampered = json.loads(json.dumps(trace))
        tampered["final_state"]["discount"] = 0.99
        with self.assertRaises(ReplayMismatchError) as ctx:
            engine.replay(facts, tampered)
        self.assertIn("final_state", ctx.exception.diffs)

    def test_replay_detects_different_input(self):
        engine = self._engine()
        trace = engine.run({"age": 20, "tier": "gold", "active": True}).trace
        with self.assertRaises(ReplayMismatchError):
            engine.replay({"age": 10, "tier": "gold", "active": True}, trace)

    def test_replay_reject_strategy_uses_exception_trace(self):
        engine = self._engine("reject")
        facts = {"age": 20, "tier": "gold", "active": True}
        with self.assertRaises(ConflictError) as ctx:
            engine.run(facts)
        # Recorded trace (from the exception) replays to the same failure.
        self.assertIsNone(engine.replay(facts, ctx.exception.trace))

    def test_trace_is_json_serializable_with_explanations(self):
        engine = self._engine()
        trace = engine.run({"age": 20, "tier": "gold", "active": True}).trace
        encoded = json.dumps(trace, sort_keys=True)
        self.assertEqual(json.loads(encoded)["field_sources"]["discount"]
                         ["source"], "vip")

    def test_format_trace_renders_explanation(self):
        engine = self._engine()
        trace = engine.run({"age": 20, "tier": "gold", "active": True}).trace
        text = "\n".join(format_trace(trace))
        self.assertIn("[matched] vip", text)
        self.assertIn("tier in ['gold']: actual='gold' -> True", text)
        self.assertIn("REJECTED: conflict, overridden_by vip", text)
        self.assertIn("basis: priority 10 > 5", text)
        self.assertIn("discount <= rule vip", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
