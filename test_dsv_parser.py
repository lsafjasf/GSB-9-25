"""test_dsv_parser.py —— dsv_parser 回归测试。"""

import unittest

from dsv_parser import (
    FormatSpec, InferenceError, ParseError,
    infer, parse, parse_chunks, StreamParser,
)


def chunks_of(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class TestInference(unittest.TestCase):
    def test_detects_common_delimiters(self):
        cases = {
            ",": "a,b,c\n1,2,3\n",
            "\t": "a\tb\tc\n1\t2\t3\n",
            ";": "a;b;c\n1;2;3\n",
            "|": "a|b|c\n1|2|3\n",
        }
        for delim, text in cases.items():
            spec = infer(text)
            self.assertEqual(spec.delimiter, delim, text)
            self.assertEqual(spec.quotechar, '"')

    def test_detects_single_quote(self):
        spec = infer("name;note\n'alice';'hi; there'\n'bob';'yo'\n")
        self.assertEqual(spec.delimiter, ";")
        self.assertEqual(spec.quotechar, "'")

    def test_rationale_is_explainable(self):
        spec = infer("a,b,c\n1,2,3\n")
        self.assertIn("','", spec.rationale)
        self.assertIn("2 record(s) x 3 field(s)", spec.rationale)
        self.assertTrue(spec.stats)  # 全部候选的统计证据可审计

    def test_single_line_consistent_with_multi_line(self):
        one = infer('"a,b";"c,d"')
        many = infer('"a,b";"c,d"\n"e,f";"g,h"\n')
        self.assertEqual((one.delimiter, one.quotechar),
                         (many.delimiter, many.quotechar))

    def test_insufficient_evidence_single_column(self):
        with self.assertRaises(InferenceError):
            infer("just\none\ncolumn\n")

    def test_insufficient_evidence_empty(self):
        with self.assertRaises(InferenceError):
            infer("")

    def test_ambiguous_delimiter_raises_not_guesses(self):
        # 逗号与分号都能切出一致的两列 -> 必须报错而非猜测
        with self.assertRaises(InferenceError) as ctx:
            infer("a,b;c\n")
        self.assertIn("ambiguous", str(ctx.exception))


class TestParsing(unittest.TestCase):
    def test_delimiter_inside_quotes(self):
        r = parse('a,b\n"x,y",z\n')
        self.assertEqual(r.rows, [["a", "b"], ["x,y", "z"]])

    def test_newline_inside_quotes(self):
        r = parse('a,b\n"line1\nline2",z\n')
        self.assertEqual(r.rows, [["a", "b"], ["line1\nline2", "z"]])

    def test_escaped_quotes(self):
        r = parse('a,b\n"say ""hi""",x\n')
        self.assertEqual(r.rows, [["a", "b"], ['say "hi"', "x"]])

    def test_empty_fields(self):
        r = parse("a,,c\n,,\n")
        self.assertEqual(r.rows, [["a", "", "c"], ["", "", ""]])

    def test_mixed_quotes(self):
        text = "name,desc\n'alice',\"hi, there\"\n'bob',\"yo\"\n"
        r = parse(text)
        self.assertEqual(r.spec.quotechar, '"')
        self.assertEqual(r.rows[1], ["'alice'", "hi, there"])

    def test_crlf_and_cr_line_endings(self):
        self.assertEqual(parse("a,b\r\n1,2\r\n").rows, [["a", "b"], ["1", "2"]])
        self.assertEqual(parse("a,b\r1,2\r").rows, [["a", "b"], ["1", "2"]])

    def test_no_trailing_phantom_record(self):
        self.assertEqual(len(parse("a,b\n1,2\n").rows), 2)
        self.assertEqual(len(parse("a,b\n1,2").rows), 2)


class TestStreamingConsistency(unittest.TestCase):
    TEXT = ('id,name,note,tag\n'
            '1,"alice, a.","line one\nline two","x""y"\n'
            '2,bob,,"plain"\n'
            '3,"carol","say ""hi""",\n')

    def test_any_chunk_size_matches_one_shot(self):
        expected = parse(self.TEXT).rows
        for size in list(range(1, 17)) + [64, 1024, len(self.TEXT)]:
            got = parse_chunks(chunks_of(self.TEXT, size)).rows
            self.assertEqual(got, expected, "chunk size %d" % size)

    def test_feed_returns_incremental_rows(self):
        spec = infer(self.TEXT)
        p = StreamParser(spec)
        rows = []
        for c in chunks_of(self.TEXT, 7):
            rows.extend(p.feed(c))
        rows.extend(p.close())
        self.assertEqual(rows, parse(self.TEXT).rows)

    def test_lenient_streaming_matches_lenient_one_shot(self):
        spec = FormatSpec(",", '"', "manual", [])
        text = "a,b\n1,2\nBROKEN\n3,4\n"
        one = parse(text, spec=spec, on_error="skip")
        for size in (1, 3, 100):
            got = parse_chunks(chunks_of(text, size), spec=spec, on_error="skip")
            self.assertEqual(got.rows, one.rows)
            self.assertEqual(got.skipped, one.skipped)


class TestErrorHandling(unittest.TestCase):
    SPEC = FormatSpec(",", '"', "manual", [])

    def test_unclosed_quote_reports_position(self):
        with self.assertRaises(ParseError) as ctx:
            parse('a,b\n1,"oops\n', spec=self.SPEC)
        self.assertEqual(ctx.exception.row, 2)
        self.assertEqual(ctx.exception.col, 2)

    def test_field_count_mismatch_reports_row(self):
        with self.assertRaises(ParseError) as ctx:
            parse("a,b\n1,2\n3,4,5\n", spec=self.SPEC)
        self.assertEqual(ctx.exception.row, 3)
        self.assertIn("expected 2, got 3", str(ctx.exception))

    def test_lenient_mode_skips_and_counts(self):
        r = parse("a,b\n1,2\nBROKEN\n3,4\n5,6,7,8\n",
                  spec=self.SPEC, on_error="skip")
        self.assertEqual(r.rows, [["a", "b"], ["1", "2"], ["3", "4"]])
        self.assertEqual(r.skipped, 2)
        self.assertEqual(len(r.errors), 2)
        self.assertEqual([e.row for e in r.errors], [3, 5])

    def test_lenient_mode_counts_unclosed_quote_at_eof(self):
        r = parse('a,b\n1,2\n3,"unclosed', spec=self.SPEC, on_error="skip")
        self.assertEqual(r.rows, [["a", "b"], ["1", "2"]])
        self.assertEqual(r.skipped, 1)
        self.assertIn("unclosed quote", str(r.errors[0]))

    def test_explicit_spec_bypasses_inference(self):
        spec = FormatSpec(";", "'", "manual", [])
        r = parse("a;'b;b'", spec=spec)
        self.assertEqual(r.rows, [["a", "b;b"]])


if __name__ == "__main__":
    unittest.main()
