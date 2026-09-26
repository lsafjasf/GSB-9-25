"""Stage 4 unit tests: error rendering (classification + position)."""

import unittest

from ql.errorgen import render
from ql.errors import (
    ErrorCode,
    bad_escape,
    duplicate_column,
    limit_range,
    unclosed_group,
    unclosed_string,
    unexpected_char,
    unexpected_token,
    where_constant,
)


class ErrorRenderTests(unittest.TestCase):
    def test_payload_shape(self):
        payload = render(unexpected_char(3, "@"), "ab @")
        self.assertEqual(payload, {
            "ok": False,
            "error": {
                "code": ErrorCode.UNEXPECTED_CHAR,
                "message": "unexpected character '@'",
                "offset": 3, "line": 1, "column": 4,
            },
        })

    def test_line_and_column_after_newlines(self):
        source = "SELECT a\nFROM t\n  @rest"
        offset = source.index("@")
        payload = render(unexpected_char(offset, "@"), source)
        self.assertEqual(payload["error"]["line"], 3)
        self.assertEqual(payload["error"]["column"], 3)
        self.assertEqual(payload["error"]["offset"], offset)

    def test_eof_error_on_empty_source(self):
        err = unexpected_token(0, None, "SELECT")  # type: ignore[arg-type]
        payload = render(err, "")
        self.assertEqual(payload["error"]["offset"], 0)
        self.assertEqual(payload["error"]["line"], 1)
        self.assertEqual(payload["error"]["column"], 1)

    def test_eof_error_after_newline(self):
        source = "SELECT a FROM t\n"
        err = unexpected_token(len(source), "end of query", "an expression")
        payload = render(err, source)
        self.assertEqual(payload["error"]["line"], 2)
        self.assertEqual(payload["error"]["column"], 1)

    def test_error_codes_are_stable(self):
        cases = [
            (unclosed_string(0), ErrorCode.UNCLOSED_STRING),
            (bad_escape(1, "q"), ErrorCode.BAD_ESCAPE),
            (unexpected_token(0, "x", "SELECT"),
             ErrorCode.UNEXPECTED_TOKEN),
            (unclosed_group(5), ErrorCode.UNCLOSED_GROUP),
            (duplicate_column(9, "a"), ErrorCode.DUPLICATE_COLUMN),
            (where_constant(3), ErrorCode.WHERE_CONSTANT),
            (limit_range(2, 1001), ErrorCode.LIMIT_RANGE),
        ]
        for err, code in cases:
            with self.subTest(code=code):
                self.assertEqual(render(err, "")["error"]["code"], code)

    def test_messages_are_worded_once(self):
        # Wording must remain byte-identical for downstream consumers.
        self.assertEqual(
            render(where_constant(0), "")["error"]["message"],
            "WHERE condition must reference at least one column",
        )
        self.assertEqual(
            render(unclosed_group(0), "")["error"]["message"],
            "unclosed parenthesised group",
        )


if __name__ == "__main__":
    unittest.main()
