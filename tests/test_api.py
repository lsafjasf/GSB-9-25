"""Facade-level tests for the four-stage composition."""

import unittest

from ql import parse_query


class ApiTests(unittest.TestCase):
    def test_success_payload_is_json_serialisable(self):
        import json
        out = parse_query('SELECT a FROM t WHERE a = 1 LIMIT 2')
        json.dumps(out)
        self.assertTrue(out["ok"])

    def test_error_payload_is_json_serialisable(self):
        import json
        out = parse_query("nonsense @")
        json.dumps(out)
        self.assertFalse(out["ok"])
        self.assertEqual(set(out["error"]),
                         {"code", "message", "offset", "line", "column"})

    def test_no_shared_state_between_calls(self):
        first = parse_query("SELECT a FROM t")
        second = parse_query("SELECT a FROM t")
        self.assertEqual(first, second)
        parse_query("SELECT a, a FROM t")  # raises internally, must not leak
        third = parse_query("SELECT a FROM t")
        self.assertEqual(third, first)


if __name__ == "__main__":
    unittest.main()
