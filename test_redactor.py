"""Unit tests for redactor.py (stdlib unittest). Run: python3 test_redactor.py"""

import json
import unittest

from redactor import Redactor, Rule, load_rules, normalize_fullwidth


def make_rule(name, pattern, priority=0, kind="mask", value="***",
              prefix="", allow_in_replaced=False, case_insensitive=False,
              order=0):
    return Rule.from_dict({
        "name": name,
        "pattern": pattern,
        "priority": priority,
        "allow_in_replaced": allow_in_replaced,
        "case_insensitive": case_insensitive,
        "replacement": {"type": kind, "value": value, "prefix": prefix},
    }, order)


class OverlapResolutionTest(unittest.TestCase):
    def test_priority_beats_declaration_order(self):
        rules = [
            make_rule("digits", r"[0-9]{6,}", priority=1, value="[NUM]", order=0),
            make_rule("phone", r"1[3-9][0-9]{9}", priority=10, value="[PHONE]", order=1),
        ]
        out, report = Redactor(rules).redact("call 13800138000 now")
        self.assertEqual(out, "call [PHONE] now")
        self.assertEqual(report["rules"][1]["replacements"], 1)
        self.assertEqual(len(report["shadowed"]), 1)
        self.assertEqual(report["shadowed"][0]["rule"], "digits")
        self.assertTrue(report["shadowed"][0]["reason"].startswith("locked-by:phone"))

    def test_longest_match_wins_within_same_priority(self):
        rules = [
            make_rule("short", r"[0-9]{5}", priority=5, value="[S]", order=0),
            make_rule("long", r"[0-9]{11}", priority=5, value="[L]", order=1),
        ]
        out, report = Redactor(rules).redact("13800138000")
        self.assertEqual(out, "[L]")
        self.assertEqual(report["shadowed"][0]["rule"], "short")

    def test_nested_email_contains_phone(self):
        rules = [
            make_rule("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                      priority=100, value="[MAIL]", order=0),
            make_rule("phone", r"1[3-9][0-9]{9}", priority=90, value="[PHONE]", order=1),
        ]
        out, report = Redactor(rules).redact("mail 13800138000@example.com!")
        self.assertEqual(out, "mail [MAIL]!")
        self.assertEqual(report["shadowed"][0]["rule"], "phone")

    def test_deterministic_across_runs(self):
        rules = [
            make_rule("email", r"\S+@\S+", priority=10, kind="pseudonym",
                      prefix="M_", order=0),
            make_rule("phone", r"1[3-9][0-9]{9}", priority=5, value="[P]", order=1),
        ]
        text = "a@b.com 13800138000 a@b.com 13900139000"
        out1, _ = Redactor(rules, key="k").redact(text)
        out2, _ = Redactor(rules, key="k").redact(text)
        self.assertEqual(out1, out2)


class PseudonymTest(unittest.TestCase):
    def setUp(self):
        self.rules = [make_rule("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+",
                                kind="pseudonym", prefix="M_")]

    def test_stable_within_batch(self):
        out, _ = Redactor(self.rules, key="k1").redact("x@y.com and x@y.com")
        tokens = out.split(" and ")
        self.assertEqual(tokens[0], tokens[1])
        self.assertTrue(tokens[0].startswith("M_"))

    def test_reproducible_across_batches_with_same_key(self):
        out1, _ = Redactor(self.rules, key="k1").redact("x@y.com")
        out2, _ = Redactor(self.rules, key="k1").redact("x@y.com")
        self.assertEqual(out1, out2)

    def test_different_key_gives_different_token(self):
        out1, _ = Redactor(self.rules, key="k1").redact("x@y.com")
        out2, _ = Redactor(self.rules, key="k2").redact("x@y.com")
        self.assertNotEqual(out1, out2)

    def test_pseudonym_requires_key(self):
        with self.assertRaises(ValueError):
            Redactor(self.rules)


class NormalizationTest(unittest.TestCase):
    def test_fullwidth_not_matched_by_default(self):
        rules = [make_rule("phone", r"1[3-9][0-9]{9}", value="[P]")]
        text = "全角１３８００１３８００不变"
        out, report = Redactor(rules).redact(text)
        self.assertEqual(out, text)
        self.assertEqual(report["summary"]["total_replacements"], 0)

    def test_fullwidth_opt_in_matches_but_preserves_other_text(self):
        rules = [make_rule("phone", r"1[3-9][0-9]{9}", value="[P]")]
        text = "电话１３８００１３８０００，其他ＡＢＣ保留"
        out, _ = Redactor(rules, normalize_fullwidth_opt=True).redact(text)
        self.assertEqual(out, "电话[P]，其他ＡＢＣ保留")

    def test_normalize_fullwidth_is_length_preserving(self):
        s = "ＡＢＣ１２３　ｘ"
        self.assertEqual(len(normalize_fullwidth(s)), len(s))
        self.assertEqual(normalize_fullwidth(s), "ABC123 x")

    def test_case_sensitive_by_default(self):
        rules = [make_rule("email", r"user@example\.com", value="[M]")]
        out, _ = Redactor(rules).redact("USER@EXAMPLE.COM")
        self.assertEqual(out, "USER@EXAMPLE.COM")

    def test_case_insensitive_opt_in(self):
        rules = [make_rule("email", r"user@example\.com", value="[M]",
                           case_insensitive=True)]
        out, _ = Redactor(rules).redact("USER@EXAMPLE.COM")
        self.assertEqual(out, "[M]")


class AllowInReplacedTest(unittest.TestCase):
    def _rules(self, allow):
        return [
            make_rule("secret", r"secret", priority=10, value="[X-12345]", order=0),
            make_rule("digits", r"[0-9]+", priority=1, value="[N]",
                      allow_in_replaced=allow, order=1),
        ]

    def test_replaced_region_locked_by_default(self):
        out, report = Redactor(self._rules(False)).redact("a secret here")
        self.assertEqual(out, "a [X-12345] here")
        digits = report["rules"][1]
        self.assertEqual(digits["replacements"], 0)

    def test_allow_in_replaced_rescans_locked_region(self):
        out, report = Redactor(self._rules(True)).redact("a secret here")
        self.assertEqual(out, "a [X-[N]] here")
        self.assertEqual(report["summary"]["total_shadowed"], 0)

    def test_original_candidate_shadowed_by_lock(self):
        rules = [
            make_rule("token", r"secret[0-9]+", priority=10, value="[X]", order=0),
            make_rule("digits", r"[0-9]+", priority=1, value="[N]", order=1),
        ]
        out, report = Redactor(rules).redact("a secret12345 here")
        self.assertEqual(out, "a [X] here")
        self.assertEqual(report["shadowed"][0]["rule"], "digits")
        self.assertEqual(report["shadowed"][0]["matched"], "12345")
        self.assertTrue(report["shadowed"][0]["reason"].startswith("locked-by:token"))


class ReportTest(unittest.TestCase):
    def test_report_structure(self):
        rules = [make_rule("phone", r"1[3-9][0-9]{9}", priority=5, value="[P]")]
        _, report = Redactor(rules).redact("13800138000")
        entry = report["rules"][0]
        self.assertEqual(entry["name"], "phone")
        self.assertEqual(entry["replacements"], 1)
        span = entry["spans"][0]
        self.assertEqual((span["start"], span["end"]), (0, 11))
        self.assertEqual(span["matched"], "13800138000")
        self.assertEqual(span["replacement"], "[P]")
        json.dumps(report, ensure_ascii=False)


class SampleRulesTest(unittest.TestCase):
    def test_sample_ruleset_end_to_end(self):
        rules = load_rules("rules.sample.json")
        text = ("联系 13800138000 或 13800138000@example.com，"
                "证件 11010119900307771X，编号 12345678。")
        out, report = Redactor(rules, key="demo").redact(text)
        self.assertIn("[PHONE]", out)
        self.assertIn("MAIL_", out)
        self.assertIn("ID_", out)
        self.assertIn("[NUM]", out)
        self.assertGreaterEqual(report["summary"]["total_shadowed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
