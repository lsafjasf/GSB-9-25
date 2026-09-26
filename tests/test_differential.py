"""Differential regression tests: refactored pipeline vs. legacy monolith.

Every case must produce the exact same payload — success structure *and*
error code/message/offset/line/column.  Two layers:

1. hand-picked normal / boundary / malformed cases;
2. seeded fuzzing (grammar-biased generation plus byte-level mutation),
   deterministic and stdlib-only.
"""

import random
import unittest

from legacy.monolith import parse_query as old_parse
from ql import parse_query as new_parse


CURATED = [
    # ---- normal --------------------------------------------------------
    "SELECT a, b FROM t WHERE a = 1 AND b <> \"x\" LIMIT 10",
    "select A fRoM TableX",
    "SELECT a FROM t WHERE a = 1 OR b = 2 AND c = 3",
    "SELECT a,b FROM t WHERE ((a = 1) AND (b = 2 OR c = 3))",
    'SELECT a FROM t WHERE a = "quote:\\" tab:\\t nl:\\n" LIMIT 0',
    "SELECT a FROM t\n  WHERE a >= 1\n  LIMIT 5 -- comment",
    # ---- boundary ------------------------------------------------------
    "",
    " ",
    "-- only a comment\n",
    "SELECT a FROM t LIMIT 0",
    "SELECT a FROM t LIMIT 1000",
    "SELECT a FROM t LIMIT -0",
    "SELECT a FROM t WHERE a",
    "SELECT a FROM t WHERE ((a))",
    # ---- malformed -----------------------------------------------------
    "SELECT",
    "SELECT FROM t",
    "SELECT a FROM",
    "SELECT a b FROM t",
    "SELECT a, FROM t",
    "SELECT a FROM t WHERE",
    "SELECT a FROM t WHERE 1 = 1",
    "SELECT a FROM t WHERE 1",
    "SELECT a FROM t WHERE (1 = 2 OR 3)",
    "SELECT a FROM t WHERE (((1)))",
    "SELECT a, a FROM t",
    "SELECT a FROM t LIMIT 1001",
    "SELECT a FROM t LIMIT -1",
    "SELECT a FROM t LIMIT",
    "SELECT a FROM t LIMIT abc",
    "SELECT a FROM t LIMIT 10 LIMIT 11",
    "SELECT a FROM t WHERE a = 1 WHERE b = 2",
    "SELECT a FROM t WHERE a = 1 LIMIT 2 WHERE b = 3",
    "SELECT a FROM t LIMIT 2 WHERE b = 3",
    "SELECT a FROM t WHERE a = 1 < 2",
    "SELECT a FROM t WHERE a =",
    "SELECT a FROM t WHERE a = 1 AND",
    "SELECT a FROM t WHERE (a = 1",
    'SELECT a FROM t WHERE a = "unclosed',
    'SELECT a FROM t WHERE a = "bad\\q"',
    "SELECT a FROM t @",
    "SELECT * FROM t",
    "SELECT a FROM t #",
    "SELECT a FROM t WHERE a = 1 EXTRA",
    'SELECT a FROM t WHERE a = "line\nbreak"',
    "SELECT a FROM t WHERE a != 1",
    "SELECT a FROM t WHERE a<>1 AND b<=2 OR c>3",
]


def _assert_equal_payload(test, text):
    expected = old_parse(text)
    actual = new_parse(text)
    test.assertEqual(
        actual, expected,
        msg=f"\ninput: {text!r}\n old: {expected}\n new: {actual}",
    )


class CuratedDifferentialTests(unittest.TestCase):
    def test_all_curated_cases_match(self):
        for text in CURATED:
            with self.subTest(text=text):
                _assert_equal_payload(self, text)


# ---------------------------------------------------------------------------
# Seeded fuzzing
# ---------------------------------------------------------------------------

ATOMS = ["a", "b", "id1", "_x", "name", "1", "0", "42", "1000", "1001",
         '"s"', '"a\\"b"', '"t\\t"', '"n\\n"', "(E)"]
OPS = ["=", "<>", "!=", "<", "<=", ">", ">="]


def _gen_expr(rng, depth):
    if depth <= 0 or rng.random() < 0.35:
        atom = rng.choice(ATOMS)
        if atom == "(E)":
            return "(" + _gen_expr(rng, depth - 1) + ")"
        return atom
    r = rng.random()
    if r < 0.55:
        left = rng.choice(ATOMS)
        if left == "(E)":
            left = "(" + _gen_expr(rng, depth - 1) + ")"
        right = rng.choice(ATOMS)
        if right == "(E)":
            right = "(" + _gen_expr(rng, depth - 1) + ")"
        return left + rng.choice(OPS) + right
    join = rng.choice([" AND ", " OR ", " and ", " or "])
    return _gen_expr(rng, depth - 1) + join + _gen_expr(rng, depth - 1)


def _gen_query(rng):
    pool = ["a", "b", "c", "name", "id1"]
    cols = [rng.choice(pool) for _ in range(rng.randint(1, 3))]
    if rng.random() < 0.1:
        cols.append(rng.choice(cols))
    query = "SELECT " + ",".join(cols)
    query += rng.choice([" FROM ", " from "]) + rng.choice(["t", "users"])
    if rng.random() < 0.8:
        query += rng.choice([" WHERE ", " where "]) + _gen_expr(rng, 2)
    if rng.random() < 0.5:
        query += rng.choice([" LIMIT ", " limit "]) + rng.choice(
            ["0", "1", "5", "1000", "1001", "-1", "-0", "12"])
    r = rng.random()
    if r < 0.08:
        query += rng.choice([" @", " #", " EXTRA", " LIMIT 3", " WHERE a=1"])
    if r < 0.16:
        pos = rng.randint(0, len(query))
        query = (query[:pos]
                 + rng.choice(["(", ")", '"', "!", "?", "*", "=", ","])
                 + query[pos:])
    if r < 0.22 and ")" in query:
        query = query.replace(")", "", 1)
    if r < 0.28:
        query = query[: rng.randint(0, len(query))]
    return query


_ALPHABET = list('SELECTFROMWHERELIMITANDORab019 ()=<>,!-"\n\t\r@#$%\\&|+.;:*?')
_SEEDS = [
    "SELECT a FROM t WHERE a = 1 LIMIT 5",
    'SELECT a,b FROM t WHERE a <> "x" AND (b >= 2 OR c)',
    "select a from",
    "",
    '"bad string \\q"',
]


def _mutate(rng, text):
    chars = list(text)
    for _ in range(rng.randint(1, 6)):
        op = rng.randrange(3)
        if op == 0 and chars:
            chars[rng.randrange(len(chars))] = rng.choice(_ALPHABET)
        elif op == 1:
            chars.insert(rng.randrange(len(chars) + 1),
                         rng.choice(_ALPHABET))
        elif chars:
            del chars[rng.randrange(len(chars))]
    return "".join(chars)


class FuzzDifferentialTests(unittest.TestCase):
    def test_grammar_biased_fuzz(self):
        rng = random.Random(20260926)
        for _ in range(4000):
            with self.subTest():
                _assert_equal_payload(self, _gen_query(rng))

    def test_byte_mutation_fuzz(self):
        rng = random.Random(99)
        for _ in range(4000):
            text = _mutate(rng, rng.choice(_SEEDS))
            with self.subTest():
                _assert_equal_payload(self, text)


if __name__ == "__main__":
    unittest.main()
