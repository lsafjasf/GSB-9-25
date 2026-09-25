import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from abac import RuleSet, RuleSetError, evaluate  # noqa: E402

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def make_ruleset(rules, config=None):
    return RuleSet.from_dict({"config": config or {}, "rules": rules})


class TestBasicDecisions(unittest.TestCase):
    def test_allow_when_rule_matches(self):
        rs = make_ruleset([
            {"id": "r1", "effect": "allow", "priority": 10,
             "when": {"action": "read", "subject": {"role": "admin"}}},
        ])
        d = evaluate(rs, {"subject": {"role": "admin"}, "action": "read"})
        self.assertEqual(d.verdict, "allow")
        self.assertEqual(d.decided_by, "r1")

    def test_default_deny_when_nothing_matches(self):
        rs = make_ruleset([
            {"id": "r1", "effect": "allow", "priority": 10,
             "when": {"action": "read"}},
        ])
        d = evaluate(rs, {"action": "write"})
        self.assertEqual(d.verdict, "deny")
        self.assertIsNone(d.decided_by)
        self.assertEqual(d.explanation["default_policy_applied"], "default_effect=deny")

    def test_higher_priority_wins(self):
        rs = make_ruleset([
            {"id": "allow", "effect": "allow", "priority": 10, "when": {"action": "read"}},
            {"id": "deny", "effect": "deny", "priority": 20, "when": {"action": "read"}},
        ])
        d = evaluate(rs, {"action": "read"})
        self.assertEqual(d.verdict, "deny")
        self.assertEqual(d.decided_by, "deny")
        shadowed = d.explanation["shadowed"]
        self.assertEqual([s["id"] for s in shadowed], ["allow"])
        self.assertIn("lower priority", shadowed[0]["reason"])

    def test_deny_overrides_tie_break(self):
        rs = make_ruleset([
            {"id": "a", "effect": "allow", "priority": 10, "when": {"action": "read"}},
            {"id": "d", "effect": "deny", "priority": 10,
             "when": {"subject": {"status": "terminated"}}},
        ])
        d = evaluate(rs, {"action": "read", "subject": {"status": "terminated"}})
        self.assertEqual(d.verdict, "deny")
        self.assertEqual(d.decided_by, "d")
        self.assertEqual(d.explanation["tie_break"], "deny-overrides")
        self.assertEqual(d.explanation["shadowed"][0]["id"], "a")

    def test_matched_and_not_matched_listed(self):
        rs = make_ruleset([
            {"id": "hit", "effect": "allow", "priority": 1, "when": {"action": "read"}},
            {"id": "miss", "effect": "allow", "priority": 1,
             "when": {"action": "read", "subject": {"clearance": {"gte": 5}}}},
        ])
        d = evaluate(rs, {"action": "read", "subject": {"clearance": 1}})
        self.assertEqual([m["id"] for m in d.explanation["matched"]], ["hit"])
        self.assertEqual(d.explanation["not_matched"][0]["id"], "miss")
        self.assertIn("clearance", d.explanation["not_matched"][0]["reason"])


class TestOperators(unittest.TestCase):
    def test_ref_comparison(self):
        rs = make_ruleset([
            {"id": "owner", "effect": "allow", "priority": 1,
             "when": {"resource": {"owner": {"eq": "$subject.id"}}}},
        ])
        req = {"subject": {"id": "alice"}, "resource": {"owner": "alice"}}
        self.assertEqual(evaluate(rs, req).verdict, "allow")
        req["resource"]["owner"] = "bob"
        self.assertEqual(evaluate(rs, req).verdict, "deny")

    def test_between_and_prefix_and_contains(self):
        rs = make_ruleset([
            {"id": "r", "effect": "allow", "priority": 1,
             "when": {"env": {"hour": {"between": [9, 17]}},
                      "resource": {"path": {"prefix": "/pub/"}},
                      "subject": {"groups": {"contains": "staff"}}}},
        ])
        good = {"env": {"hour": 10}, "resource": {"path": "/pub/a"},
                "subject": {"groups": ["staff"]}}
        self.assertEqual(evaluate(rs, good).verdict, "allow")
        bad = dict(good, env={"hour": 22})
        self.assertEqual(evaluate(rs, bad).verdict, "deny")


class TestErrorHandling(unittest.TestCase):
    def test_missing_attribute_returns_error_not_allow(self):
        rs = make_ruleset([
            {"id": "r", "effect": "allow", "priority": 1,
             "when": {"subject": {"clearance": {"gte": 3}}}},
        ])
        d = evaluate(rs, {"subject": {}})
        self.assertEqual(d.verdict, "error")
        self.assertEqual(d.explanation["errored"][0]["error_type"], "AttributeMissingError")
        self.assertEqual(d.explanation["default_policy_applied"], "on_attribute_error=error")

    def test_type_mismatch_returns_error(self):
        rs = make_ruleset([
            {"id": "r", "effect": "allow", "priority": 1,
             "when": {"subject": {"clearance": {"gte": 3}}}},
        ])
        d = evaluate(rs, {"subject": {"clearance": "high"}})
        self.assertEqual(d.verdict, "error")
        self.assertEqual(d.explanation["errored"][0]["error_type"], "ConditionTypeError")

    def test_configured_deny_policy_on_error(self):
        rs = make_ruleset(
            [{"id": "r", "effect": "allow", "priority": 1,
              "when": {"subject": {"clearance": {"gte": 3}}}}],
            config={"on_attribute_error": "deny"},
        )
        d = evaluate(rs, {"subject": {}})
        self.assertEqual(d.verdict, "deny")
        self.assertEqual(d.explanation["default_policy_applied"], "on_attribute_error=deny")

    def test_exists_operator_handles_optional_attrs(self):
        rs = make_ruleset([
            {"id": "r", "effect": "deny", "priority": 1,
             "when": {"subject": {"mfa": {"exists": False}}}},
        ])
        d = evaluate(rs, {"subject": {}})
        self.assertEqual(d.verdict, "deny")
        self.assertEqual(d.decided_by, "r")


class TestLoadTimeValidation(unittest.TestCase):
    def test_contradiction_detected(self):
        with self.assertRaises(RuleSetError) as ctx:
            make_ruleset([
                {"id": "a", "effect": "allow", "priority": 10,
                 "when": {"action": "read"}},
                {"id": "b", "effect": "deny", "priority": 10,
                 "when": {"action": "read"}},
            ])
        self.assertIn("contradictory", str(ctx.exception))

    def test_same_conditions_different_priority_is_not_contradiction(self):
        rs = make_ruleset([
            {"id": "a", "effect": "allow", "priority": 10, "when": {"action": "read"}},
            {"id": "b", "effect": "deny", "priority": 20, "when": {"action": "read"}},
        ])
        self.assertEqual(evaluate(rs, {"action": "read"}).decided_by, "b")

    def test_duplicate_id_rejected(self):
        with self.assertRaises(RuleSetError):
            make_ruleset([
                {"id": "a", "effect": "allow", "priority": 1, "when": {"action": "read"}},
                {"id": "a", "effect": "allow", "priority": 2, "when": {"action": "read"}},
            ])

    def test_invalid_operator_rejected(self):
        with self.assertRaises(ValueError):
            make_ruleset([
                {"id": "a", "effect": "allow", "priority": 1,
                 "when": {"action": {"frobnicate": "x"}}},
            ])


class TestIndexConsistency(unittest.TestCase):
    def test_indexed_matches_linear_scan(self):
        rng = random.Random(7)
        for trial in range(20):
            n = rng.randint(20, 200)
            rules = []
            for i in range(n):
                when = {"action": {"eq": "a%d" % rng.randint(0, 5)}}
                if rng.random() < 0.5:
                    when["subject"] = {"clearance": {"gte": rng.randint(0, 4)}}
                if rng.random() < 0.3:
                    when["resource"] = {"type": {"eq": "t%d" % rng.randint(0, 3)}}
                # Derive the effect from the conditions so that identical
                # conditions never produce contradictory effects.
                effect = "allow" if hash(repr(sorted(when.items()))) % 2 == 0 else "deny"
                rules.append({"id": "r%d_%d" % (trial, i),
                              "effect": effect,
                              "priority": rng.randint(0, 9), "when": when})
            rs = make_ruleset(rules)
            for _ in range(20):
                req = {"action": "a%d" % rng.randint(0, 5),
                       "subject": {"clearance": rng.randint(0, 5)},
                       "resource": {"type": "t%d" % rng.randint(0, 3)}}
                fast = evaluate(rs, req, use_index=True).to_dict()
                slow = evaluate(rs, req, use_index=False).to_dict()
                # not_matched legitimately differs: the index skips rules
                # that cannot match, so they are never evaluated/listed.
                for key in ("decision", "decided_by", "matched", "shadowed",
                            "errored", "tie_break", "default_policy_applied"):
                    self.assertEqual(fast[key], slow[key], key)


class TestExamples(unittest.TestCase):
    def _load(self, name):
        with open(os.path.join(EXAMPLES, name), encoding="utf-8") as fh:
            return json.load(fh)

    def test_example_allow(self):
        rs = RuleSet.load(os.path.join(EXAMPLES, "rules.json"))
        d = evaluate(rs, self._load("request_allow.json"))
        self.assertEqual(d.verdict, "allow")
        self.assertEqual(d.decided_by, "allow-dept-read")

    def test_example_tiebreak_deny(self):
        rs = RuleSet.load(os.path.join(EXAMPLES, "rules.json"))
        d = evaluate(rs, self._load("request_deny_tiebreak.json"))
        self.assertEqual(d.verdict, "deny")
        self.assertEqual(d.decided_by, "deny-terminated-employee")
        self.assertEqual(d.explanation["tie_break"], "deny-overrides")
        self.assertEqual(d.explanation["shadowed"][0]["id"], "allow-admin-anything")

    def test_example_missing_attr(self):
        rs = RuleSet.load(os.path.join(EXAMPLES, "rules.json"))
        d = evaluate(rs, self._load("request_missing_attr.json"))
        self.assertEqual(d.verdict, "error")
        self.assertTrue(d.explanation["errored"])

    def test_conflict_example_rejected(self):
        with self.assertRaises(RuleSetError):
            RuleSet.load(os.path.join(EXAMPLES, "conflict_rules.json"))


if __name__ == "__main__":
    unittest.main()
