"""Recovery-mode tests: partial results + error list, strict-mode parity.

Key invariants pinned here:

1. valid input   -> recovery result identical to strict, empty error list;
2. invalid input -> partial result plus an error list whose entries carry
   code/message/offset/line/column/expected/actual;
3. prefix        -> appending garbage to a valid query never changes the
   recovered query fields;
4. strict error  -> always appears in the recovery error list (same
   code/offset/message), for every fuzzed input.
"""

import json
import random
import unittest

from ql import parse_query
from tests.test_differential import CURATED, _gen_query, _mutate, _SEEDS


def recover(text):
    return parse_query(text, mode="recover")


def strict(text):
    return parse_query(text)  # default mode must stay strict


class RecoveryShapeTests(unittest.TestCase):
    def test_valid_input_matches_strict_exactly(self):
        text = 'SELECT a, b FROM t WHERE a = 1 AND b <> "x" LIMIT 10'
        out = recover(text)
        self.assertTrue(out["ok"])
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["query"], strict(text)["query"])

    def test_payload_is_json_serialisable(self):
        out = recover('SELECT a, 1, b FROM t WHERE a = @ LIMIT 9999')
        json.dumps(out)
        self.assertFalse(out["ok"])
        for entry in out["errors"]:
            self.assertEqual(
                set(entry),
                {"code", "message", "offset", "line", "column",
                 "expected", "actual"},
            )

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            parse_query("SELECT a FROM t", mode="lenient")

    def test_default_mode_is_strict(self):
        text = "SELECT a FROM t WHERE a = 1 EXTRA"
        self.assertEqual(parse_query(text), parse_query(text, mode="strict"))
        self.assertFalse(parse_query(text)["ok"])
        self.assertIn("error", parse_query(text))
        self.assertNotIn("errors", parse_query(text))


class RecoveryContentTests(unittest.TestCase):
    def test_bad_column_is_skipped_rest_recovered(self):
        out = recover("SELECT a, 1, b FROM t WHERE a = 2 LIMIT 5")
        self.assertEqual(out["query"], {
            "select": ["a", "b"],
            "from": "t",
            "where": {"type": "cmp", "op": "=",
                      "left": {"type": "ref", "name": "a"},
                      "right": {"type": "literal", "value": 2}},
            "limit": 5,
        })
        self.assertEqual(len(out["errors"]), 1)
        err = out["errors"][0]
        self.assertEqual(err["code"], "E_UNEXPECTED_TOKEN")
        self.assertEqual(err["expected"], "a column name")
        self.assertEqual(err["actual"], "1")
        self.assertEqual((err["line"], err["column"]), (1, 11))

    def test_multiple_errors_all_reported_in_order(self):
        out = recover("SELECT a, 1, b FROM t WHERE a = @ LIMIT 9999 LIMIT 5")
        codes = [e["code"] for e in out["errors"]]
        self.assertEqual(codes, [
            "E_UNEXPECTED_TOKEN",   # column `1`
            "E_UNEXPECTED_CHAR",    # `@`
            "E_UNEXPECTED_TOKEN",   # `a = LIMIT`: expression missing
            "E_LIMIT_RANGE",        # 9999
            "E_UNEXPECTED_TOKEN",   # second LIMIT
        ])
        offsets = [e["offset"] for e in out["errors"]]
        self.assertEqual(offsets, sorted(offsets))
        # valid clauses still landed in the partial result
        self.assertEqual(out["query"]["select"], ["a", "b"])
        self.assertEqual(out["query"]["from"], "t")
        self.assertIsNone(out["query"]["where"])   # dropped: `a =` incomplete
        self.assertIsNone(out["query"]["limit"])   # dropped: 9999 out of range

    def test_error_list_pinpoints_line_and_column(self):
        text = (
            "SELECT a, b\n"
            "FROM t\n"
            "WHERE a = 1 AND\n"
            "  b = # oops\n"
            "LIMIT 5"
        )
        out = recover(text)
        self.assertEqual(len(out["errors"]), 1)
        err = out["errors"][0]
        self.assertEqual(err["code"], "E_UNEXPECTED_CHAR")
        self.assertEqual(err["actual"], None)  # lexical errors have no pair
        self.assertEqual((err["line"], err["column"]), (4, 7))
        self.assertEqual(err["offset"], text.index("#"))
        # `#` skipped, `oops` survives as an identifier, LIMIT still parsed
        self.assertEqual(out["query"]["limit"], 5)
        self.assertEqual(out["query"]["where"], {
            "type": "and",
            "args": [
                {"type": "cmp", "op": "=",
                 "left": {"type": "ref", "name": "a"},
                 "right": {"type": "literal", "value": 1}},
                {"type": "cmp", "op": "=",
                 "left": {"type": "ref", "name": "b"},
                 "right": {"type": "ref", "name": "oops"}},
            ],
        })

    def test_expected_actual_pair_on_structural_errors(self):
        out = recover("SELECT a FROM t LIMIT 2 WHERE b = 3")
        err = out["errors"][0]
        self.assertEqual(err["expected"], "end of query")
        self.assertEqual(err["actual"], "WHERE")
        self.assertEqual((err["line"], err["column"]), (1, 25))
        self.assertEqual(out["query"]["limit"], 2)
        self.assertIsNone(out["query"]["where"])

    def test_unclosed_string_skipped_scanning_continues(self):
        out = recover('SELECT a FROM t WHERE a = "unclosed\nLIMIT 3')
        codes = [e["code"] for e in out["errors"]]
        self.assertIn("E_UNCLOSED_STRING", codes)
        self.assertEqual(out["query"]["limit"], 3)

    def test_semantic_errors_collected_not_raised(self):
        out = recover("SELECT a, a FROM t WHERE 1 = 1 LIMIT 1001")
        codes = [e["code"] for e in out["errors"]]
        self.assertEqual(codes, [
            "E_DUPLICATE_COLUMN", "E_WHERE_CONSTANT", "E_LIMIT_RANGE",
        ])
        self.assertEqual(out["query"], {
            "select": ["a"], "from": "t", "where": None, "limit": None,
        })


class PrefixConsistencyTests(unittest.TestCase):
    """Recovery on `valid + garbage` must keep the strict result intact."""

    VALID = [
        "SELECT a FROM t",
        "SELECT a, b FROM t WHERE a = 1 LIMIT 10",
        'SELECT x FROM users WHERE x <> "s" OR x = 2',
        "SELECT a FROM t LIMIT 0",
    ]
    # Note: suffixes must not extend a trailing clause of the valid prefix
    # (e.g. " AND" would be absorbed by a WHERE expression; " LIMIT 1"
    # would add a genuinely valid clause).  Those cases are covered by the
    # content tests instead.
    GARBAGE = [" @", " ##", ' "unclosed', " EXTRA", " WHERE 1 = 1", " )"]

    def test_valid_prefix_result_preserved(self):
        for valid in self.VALID:
            expected = strict(valid)["query"]
            for junk in self.GARBAGE:
                text = valid + junk
                with self.subTest(text=text):
                    out = recover(text)
                    self.assertFalse(out["ok"])
                    self.assertTrue(out["errors"])
                    self.assertEqual(out["query"], expected)

    def test_every_curated_valid_case_recovers_identically(self):
        for text in CURATED:
            strict_out = strict(text)
            if not strict_out["ok"]:
                continue
            with self.subTest(text=text):
                out = recover(text)
                self.assertTrue(out["ok"])
                self.assertEqual(out["errors"], [])
                self.assertEqual(out["query"], strict_out["query"])


class RecoveryFuzzTests(unittest.TestCase):
    """Seeded fuzz: recovery never crashes and never loses strict's error."""

    def _check(self, text):
        strict_out = strict(text)
        out = recover(text)
        json.dumps(out)  # always serialisable
        if strict_out["ok"]:
            self.assertTrue(out["ok"], msg=f"\ninput: {text!r}\n{out}")
            self.assertEqual(out["errors"], [])
            self.assertEqual(out["query"], strict_out["query"])
        else:
            want = strict_out["error"]
            got = [
                (e["code"], e["offset"], e["message"]) for e in out["errors"]
            ]
            self.assertIn(
                (want["code"], want["offset"], want["message"]), got,
                msg=f"\ninput: {text!r}\nstrict: {want}\nrecovery: {got}",
            )
            for entry in out["errors"]:  # positions always resolvable
                self.assertGreaterEqual(entry["line"], 1)
                self.assertGreaterEqual(entry["column"], 1)

    def test_grammar_biased_fuzz(self):
        rng = random.Random(20260929)
        for _ in range(4000):
            with self.subTest():
                self._check(_gen_query(rng))

    def test_byte_mutation_fuzz(self):
        rng = random.Random(4242)
        for _ in range(4000):
            with self.subTest():
                self._check(_mutate(rng, rng.choice(_SEEDS)))


if __name__ == "__main__":
    unittest.main()
