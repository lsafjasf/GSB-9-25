import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from abac import Policy, RuleConflictError, RuleValidationError, load_rules
from abac.bench import generate_rules

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def make_policy(rules, **kwargs):
    kwargs.setdefault("on_missing_attribute", "skip")
    return Policy.from_json(rules, **kwargs)


def rule(rid, effect, priority, **conditions):
    return {"id": rid, "effect": effect, "priority": priority,
            "conditions": conditions}


class TestDecision(unittest.TestCase):
    def test_highest_priority_wins(self):
        policy = make_policy([
            rule("low-allow", "allow", 10, action="read",
                 subject={"role": "user"}),
            rule("high-deny", "deny", 20, action="read",
                 resource={"type": "doc"}),
        ])
        result = policy.decide({"action": "read", "subject": {"role": "user"},
                                "resource": {"type": "doc"}})
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(result["deciding_rule"], "high-deny")
        shadowed = result["explanation"]["shadowed_rules"]
        self.assertEqual([s["id"] for s in shadowed], ["low-allow"])

    def test_deny_overrides_on_tie(self):
        policy = make_policy([
            rule("a-allow", "allow", 10, action="read",
                 subject={"role": "auditor"}),
            rule("b-deny", "deny", 10, action="read",
                 subject={"role": ["auditor", "admin"]}),
        ])
        result = policy.decide({"action": "read", "subject": {"role": "auditor"}})
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(result["deciding_rule"], "b-deny")
        cause = result["explanation"]["shadowed_rules"][0]["cause"]
        self.assertIn("deny-overrides", cause)

    def test_deterministic_same_effect_tie(self):
        rules = [
            rule("z-rule", "allow", 10, action="read", subject={"role": "x"}),
            rule("a-rule", "allow", 10, action="read",
                 subject={"role": ["x", "y"]}),
        ]
        req = {"action": "read", "subject": {"role": "x"}}
        first = make_policy(rules).decide(req)["deciding_rule"]
        second = make_policy(list(reversed(rules))).decide(req)["deciding_rule"]
        self.assertEqual(first, "a-rule")
        self.assertEqual(first, second)

    def test_default_deny_when_nothing_matches(self):
        policy = make_policy([rule("r", "allow", 1, action="read")])
        result = policy.decide({"action": "write"})
        self.assertEqual(result["decision"], "deny")
        self.assertIsNone(result["deciding_rule"])
        self.assertIn("default", result["explanation"]["default_applied"])

    def test_explanation_lists_matched_and_unmatched(self):
        policy = make_policy([
            rule("hit", "allow", 10, action="read", subject={"role": "x"}),
            rule("miss", "allow", 10, action="read", subject={"role": "y"}),
        ])
        exp = policy.decide({"action": "read", "subject": {"role": "x"}})["explanation"]
        self.assertEqual([m["id"] for m in exp["matched_rules"]], ["hit"])
        self.assertEqual([u["id"] for u in exp["unmatched_rules"]], ["miss"])


class TestMissingAttribute(unittest.TestCase):
    RULES = [rule("needs-role", "allow", 10, action="read",
                  subject={"role": "admin"})]

    def test_missing_attribute_errors_by_default(self):
        policy = Policy.from_json(self.RULES)  # default: error
        result = policy.decide({"action": "read", "subject": {}})
        self.assertEqual(result["decision"], "error")
        problem = result["explanation"]["attribute_problem"]
        self.assertEqual(problem["kind"], "missing")
        self.assertEqual(problem["attribute"], "subject.role")
        self.assertEqual(problem["policy_applied"], "error")

    def test_missing_attribute_deny_policy(self):
        policy = make_policy(self.RULES, on_missing_attribute="deny")
        result = policy.decide({"action": "read", "subject": {}})
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(result["explanation"]["attribute_problem"]["policy_applied"],
                         "deny")

    def test_missing_attribute_skip_policy(self):
        policy = make_policy(self.RULES, on_missing_attribute="skip")
        result = policy.decide({"action": "read", "subject": {}})
        self.assertEqual(result["decision"], "deny")  # default effect
        self.assertIn("skipped", result["explanation"]["unmatched_rules"][0]["reason"])

    def test_type_mismatch_never_allows(self):
        policy = Policy.from_json(self.RULES)  # role given as number
        result = policy.decide({"action": "read", "subject": {"role": 42}})
        self.assertEqual(result["decision"], "error")
        self.assertEqual(result["explanation"]["attribute_problem"]["kind"], "type")

    def test_missing_index_attribute(self):
        policy = Policy.from_json(self.RULES)
        result = policy.decide({"subject": {"role": "admin"}})  # no action
        self.assertEqual(result["decision"], "error")
        self.assertEqual(result["explanation"]["attribute_problem"]["attribute"],
                         "action")


class TestConflictDetection(unittest.TestCase):
    def test_opposite_effects_same_conditions_rejected(self):
        rules = [
            rule("a", "allow", 1, action="read", subject={"role": "x"}),
            rule("b", "deny", 2, subject={"role": "x"}, action="read"),
        ]
        with self.assertRaises(RuleConflictError) as ctx:
            make_policy(rules)
        self.assertIn("a", str(ctx.exception))
        self.assertIn("b", str(ctx.exception))

    def test_list_order_insensitive(self):
        rules = [
            rule("a", "allow", 1, action=["read", "write"]),
            rule("b", "deny", 1, action=["write", "read"]),
        ]
        with self.assertRaises(RuleConflictError):
            make_policy(rules)

    def test_different_conditions_no_conflict(self):
        rules = [
            rule("a", "allow", 1, action="read"),
            rule("b", "deny", 1, action="write"),
        ]
        make_policy(rules)  # must not raise

    def test_conflict_example_file(self):
        with open(os.path.join(EXAMPLES, "rules_conflict.json")) as fh:
            data = json.load(fh)
        with self.assertRaises(RuleConflictError):
            make_policy(data)


class TestValidation(unittest.TestCase):
    def test_duplicate_id(self):
        with self.assertRaises(RuleValidationError):
            load_rules([rule("x", "allow", 1), rule("x", "deny", 2)])

    def test_bad_operator(self):
        with self.assertRaises(RuleValidationError):
            load_rules([rule("x", "allow", 1,
                             subject={"a": {"op": "regex", "value": ".*"}})])

    def test_bad_effect(self):
        with self.assertRaises(RuleValidationError):
            load_rules([rule("x", "maybe", 1)])


class TestIndexAndScale(unittest.TestCase):
    def test_index_prunes_candidates(self):
        policy = make_policy(generate_rules(10000))
        result = policy.decide({
            "action": "action_3",
            "subject": {"role": "role_3", "clearance": 10 ** 9},
            "resource": {"type": "type_3", "classification": 0},
            "environment": {"hour": 12},
        })
        exp = result["explanation"]
        self.assertEqual(exp["total_rules"], 10000)
        self.assertLessEqual(exp["candidate_rules"], 10000 // 1000 + 200)
        self.assertEqual(exp["pruned_by_index"],
                         10000 - exp["candidate_rules"])

    def test_generated_set_is_conflict_free(self):
        make_policy(generate_rules(5000))  # must not raise


if __name__ == "__main__":
    unittest.main()
