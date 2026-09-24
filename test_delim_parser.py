"""Regression & reproduction tests for delim_parser (stdlib unittest).

Run: python3 -m unittest test_delim_parser -v
"""

import unittest

from delim_parser import (
    DelimParseError,
    Dialect,
    InferenceError,
    StreamParser,
    infer_dialect,
    parse,
)


# ---------------------------------------------------------------------------
# "Before" implementations: they demonstrate the four production bug classes.
# The BugReproduction tests first show the bug reproducing on the naive code,
# then show the fixed parser producing the correct result on the same input.
# ---------------------------------------------------------------------------

def naive_parse(text, delimiter=','):
    """Buggy reference: splits physical lines on the delimiter."""
    return [line.split(delimiter) for line in text.splitlines()]


def naive_infer(text):
    """Buggy reference: needs >1 row, otherwise falls back to a default."""
    lines = text.splitlines()
    if len(lines) < 2:
        return ','  # not enough data -> silently guess
    counts = {d: sum(line.count(d) for line in lines)
              for d in (',', ';', '\t', '|')}
    return max(counts, key=counts.get)


# Sample inputs covering quotes, escapes, embedded newlines, empty fields,
# and mixed quote characters.
QUOTED_DELIM = 'name,note\nAlice,"likes, commas"\n'
QUOTED_NEWLINE = 'id,txt\n1,"line one\nline two"\n2,plain\n'
ESCAPED_QUOTE = 'a,b\n"say ""hi""",x\n'
MIXED_QUOTES = "name,quote\nO'Brien,\"it's fine\"\n"
EMPTY_FIELDS = 'a,b,c\n1,,3\n,,\n'
CRLF = 'a,b\r\n1,2\r\n'
BLANK_LINES = 'a,b\n\n1,2\n'
NO_TRAILING_NEWLINE = 'a,b\n1,2'
SEMICOLON = 'a;b;c\n1;2;3\n'


class BugReproductionTests(unittest.TestCase):
    """Each test: naive code reproduces the bug; fixed parser is correct."""

    def test_bug1_delimiter_inside_quoted_field(self):
        # Bug: field containing the delimiter gets split.
        self.assertEqual(naive_parse(QUOTED_DELIM)[1],
                         ['Alice', '"likes', ' commas"'])
        res = parse(QUOTED_DELIM)
        self.assertEqual(res.records,
                         [['name', 'note'], ['Alice', 'likes, commas']])

    def test_bug2_newline_inside_quotes(self):
        # Bug: newline inside quotes ends the record early.
        self.assertEqual(len(naive_parse(QUOTED_NEWLINE)), 4)
        res = parse(QUOTED_NEWLINE)
        self.assertEqual(len(res.records), 3)
        self.assertEqual(res.records[1], ['1', 'line one\nline two'])
        self.assertEqual(res.records[2], ['2', 'plain'])

    def test_bug3_escaped_quote(self):
        # Bug: doubled quote not unescaped / miscounts fields.
        self.assertEqual(naive_parse(ESCAPED_QUOTE)[1], ['"say ""hi"""', 'x'])
        res = parse(ESCAPED_QUOTE)
        self.assertEqual(res.records, [['a', 'b'], ['say "hi"', 'x']])

    def test_bug4_single_line_inference_matches_multi_line(self):
        single = 'a;b;c\n'
        multi = 'a;b;c\n1;2;3\n4;5;6\n'
        # Bug: naive inference falls back to ',' on a single line.
        self.assertEqual(naive_infer(single), ',')
        self.assertEqual(naive_infer(multi), ';')
        # Fixed: identical scoring path for any row count.
        self.assertEqual(infer_dialect(single).dialect,
                         infer_dialect(multi).dialect)
        self.assertEqual(infer_dialect(single).dialect.delimiter, ';')
        # Also with quoted content on a single line.
        d = infer_dialect('a,"b,c",d\n').dialect
        self.assertEqual((d.delimiter, d.quotechar), (',', '"'))


class InferenceTests(unittest.TestCase):
    def test_explanation_mentions_delimiter_quote_and_evidence(self):
        res = parse(QUOTED_DELIM)
        self.assertIn("chosen delimiter=','", res.explanation)
        self.assertIn('quotechar', res.explanation)
        self.assertIn('consistent', res.explanation)
        self.assertIn('Rejected candidates', res.explanation)

    def test_infer_semicolon_tab_pipe(self):
        self.assertEqual(infer_dialect(SEMICOLON).dialect.delimiter, ';')
        self.assertEqual(infer_dialect('a\tb\n1\t2\n').dialect.delimiter, '\t')
        self.assertEqual(infer_dialect('a|b\n1|2\n').dialect.delimiter, '|')

    def test_insufficient_evidence_raises_not_guesses(self):
        with self.assertRaises(InferenceError) as ctx:
            infer_dialect('just some words\nanother line\n')
        self.assertIn('insufficient evidence', str(ctx.exception))

    def test_empty_input_raises(self):
        for text in ('', '   \n  \n'):
            with self.assertRaises(InferenceError):
                infer_dialect(text)

    def test_ambiguous_evidence_raises(self):
        # Both ':' and ',' split every row into exactly 2 fields.
        with self.assertRaises(InferenceError) as ctx:
            infer_dialect('a:b,c\n1:2,3\n')
        self.assertIn('ambiguous', str(ctx.exception))

    def test_mixed_quotes_prefers_double_quote(self):
        res = parse(MIXED_QUOTES)
        self.assertEqual(res.dialect.quotechar, '"')
        self.assertEqual(res.records,
                         [['name', 'quote'], ["O'Brien", "it's fine"]])


class StreamingConsistencyTests(unittest.TestCase):
    """Same content, any chunk size: results must equal whole-buffer parse."""

    TEXTS = [
        QUOTED_DELIM,
        QUOTED_NEWLINE,
        ESCAPED_QUOTE,
        MIXED_QUOTES,
        EMPTY_FIELDS,
        CRLF,
        BLANK_LINES,
        NO_TRAILING_NEWLINE,
        SEMICOLON,
        'a,b\n"",x\n',            # quoted empty field
        'a,b,\n1,2,\n',           # trailing empty field
        'k,v\n"a""b",c\n',        # escaped quote right after opening quote
    ]

    def _stream_records(self, text, dialect, chunk_size):
        parser = StreamParser(dialect)
        records = []
        for i in range(0, len(text), chunk_size):
            records.extend(parser.feed(text[i:i + chunk_size]))
        records.extend(parser.close())
        return records

    def test_all_chunk_sizes_match_whole_parse(self):
        for text in self.TEXTS:
            dialect = infer_dialect(text).dialect
            whole = parse(text, dialect).records
            for size in range(1, len(text) + 1):
                with self.subTest(text=text[:20], chunk_size=size):
                    self.assertEqual(
                        self._stream_records(text, dialect, size), whole)

    def test_feed_return_values_concatenate_to_whole(self):
        # Byte-at-a-time feeding must not lose or duplicate records.
        text = QUOTED_NEWLINE
        dialect = infer_dialect(text).dialect
        parser = StreamParser(dialect)
        pieces = [parser.feed(ch) for ch in text]
        pieces.append(parser.close())
        self.assertEqual([r for batch in pieces for r in batch],
                         parse(text, dialect).records)


class ErrorHandlingTests(unittest.TestCase):
    def test_unclosed_quote_strict_reports_position(self):
        dialect = Dialect(',', '"')
        with self.assertRaises(DelimParseError) as ctx:
            parse('a,b\n"oops,x\n', dialect)
        err = ctx.exception
        self.assertIn('unclosed quote', str(err))
        self.assertEqual((err.row, err.col), (2, 1))

    def test_unclosed_quote_lenient_skips_and_counts(self):
        res = parse('a,b\n"oops,x\n', Dialect(',', '"'), on_error='skip')
        self.assertEqual(res.records, [['a', 'b']])
        self.assertEqual(res.skipped, 1)
        self.assertEqual(len(res.errors), 1)
        self.assertIn('unclosed quote', str(res.errors[0]))

    def test_inconsistent_field_count_strict_reports_row(self):
        dialect = Dialect(',', '"')
        with self.assertRaises(DelimParseError) as ctx:
            parse('a,b\n1,2,3\n4,5\n', dialect)
        self.assertEqual(ctx.exception.row, 2)
        self.assertIn('expected 2 fields, got 3', str(ctx.exception))

    def test_inconsistent_field_count_lenient_skips_and_counts(self):
        res = parse('a,b\n1,2,3\n4,5\n', Dialect(',', '"'), on_error='skip')
        self.assertEqual(res.records, [['a', 'b'], ['4', '5']])
        self.assertEqual(res.skipped, 1)
        self.assertEqual(res.errors[0].row, 2)

    def test_garbage_after_closing_quote(self):
        dialect = Dialect(',', '"')
        with self.assertRaises(DelimParseError) as ctx:
            parse('a,b\n"x"y,z\n', dialect)
        self.assertIn('after closing quote', str(ctx.exception))
        self.assertEqual(ctx.exception.row, 2)

    def test_invalid_on_error_rejected(self):
        with self.assertRaises(ValueError):
            StreamParser(Dialect(',', '"'), on_error='ignore')


class RegressionTests(unittest.TestCase):
    def test_empty_fields(self):
        res = parse(EMPTY_FIELDS)
        self.assertEqual(res.records,
                         [['a', 'b', 'c'], ['1', '', '3'], ['', '', '']])

    def test_quoted_empty_field(self):
        self.assertEqual(parse('a,b\n"",x\n').records,
                         [['a', 'b'], ['', 'x']])

    def test_crlf_line_endings(self):
        self.assertEqual(parse(CRLF).records, [['a', 'b'], ['1', '2']])

    def test_blank_lines_skipped(self):
        self.assertEqual(parse(BLANK_LINES).records,
                         [['a', 'b'], ['1', '2']])

    def test_no_trailing_newline_no_phantom_record(self):
        res = parse(NO_TRAILING_NEWLINE)
        self.assertEqual(res.records, [['a', 'b'], ['1', '2']])
        self.assertEqual(parse('a,b\n').records, [['a', 'b']])

    def test_trailing_delimiter_yields_empty_field(self):
        self.assertEqual(parse('a,b,\n1,2,\n').records,
                         [['a', 'b', ''], ['1', '2', '']])

    def test_quote_midfield_is_literal(self):
        self.assertEqual(parse('a,b\nx"y,z\n').records,
                         [['a', 'b'], ['x"y', 'z']])

    def test_explicit_dialect_skips_inference(self):
        res = parse('a;b\n1;2\n', Dialect(';', '"'))
        self.assertIsNone(res.explanation)
        self.assertEqual(res.records, [['a', 'b'], ['1', '2']])

    def test_lenient_streaming_counts_match_whole_parse(self):
        text = 'a,b\n1,2,3\n"bad,x\n'
        dialect = Dialect(',', '"')
        whole = parse(text, dialect, on_error='skip')
        parser = StreamParser(dialect, on_error='skip')
        got = []
        for ch in text:
            got.extend(parser.feed(ch))
        got.extend(parser.close())
        self.assertEqual(got, whole.records)
        self.assertEqual(parser.skipped, whole.skipped)
        self.assertEqual(len(parser.errors), len(whole.errors))


if __name__ == '__main__':
    unittest.main()
