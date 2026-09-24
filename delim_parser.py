"""Delimiter/quote-inferring delimited-text parser (Python 3, stdlib only).

Fixes four production bug classes:
  1. Delimiters inside quoted fields must not split the field.
  2. Newlines inside quotes must not terminate the record.
  3. Escaped quotes (doubled, e.g. "") must unescape to a literal quote.
  4. Inference on a single row must use the exact same scoring path as
     multi-row inference, so the conclusion cannot diverge.

Inference is explainable: infer_dialect() returns the chosen delimiter,
quote character and a human-readable rationale with per-candidate stats.
When the statistical evidence is insufficient (no delimiter structure,
ambiguous tie, empty input) it raises InferenceError instead of guessing.

Streaming and whole-buffer parsing are byte-for-byte identical: the core
is a char-level state machine fed by chunks of any size.

Invalid input handling:
  - strict mode (default): raises DelimParseError with row/col.
  - lenient mode (on_error='skip'): skips the bad record, counts it in
    ParseResult.skipped and records the error in ParseResult.errors.
    Nothing is ever silently dropped.

Run tests:          python3 -m unittest test_delim_parser -v
Run inference demo: python3 delim_parser.py
"""

DELIMITER_CANDIDATES = (',', ';', '\t', '|', ':')
QUOTE_CANDIDATES = ('"', "'")


class DelimParseError(Exception):
    """Parse error with 1-based row/col position."""

    def __init__(self, message, row, col):
        super().__init__("row %d, col %d: %s" % (row, col, message))
        self.message = message
        self.row = row
        self.col = col


class InferenceError(Exception):
    """Raised when delimiter/quote inference lacks sufficient evidence."""


class Dialect:
    __slots__ = ('delimiter', 'quotechar')

    def __init__(self, delimiter, quotechar='"'):
        self.delimiter = delimiter
        self.quotechar = quotechar

    def __eq__(self, other):
        return (isinstance(other, Dialect)
                and self.delimiter == other.delimiter
                and self.quotechar == other.quotechar)

    def __repr__(self):
        return "Dialect(delimiter=%r, quotechar=%r)" % (
            self.delimiter, self.quotechar)


class _Scanner:
    """Incremental char-level state machine.

    Emits completed records as (row, fields) tuples into self.records.
    In strict mode structural errors raise DelimParseError; otherwise they
    are appended to self.errors and the scanner recovers.
    """

    def __init__(self, delimiter, quotechar, strict=True):
        self.delimiter = delimiter
        self.quotechar = quotechar
        self.strict = strict
        self.records = []        # list of (row, [field, ...])
        self.errors = []         # list of DelimParseError (lenient mode)
        self.skipped = 0         # records dropped due to structural errors
        self._fields = []
        self._field = []
        self._field_started = False
        self._in_quotes = False
        self._after_quote = False
        self._quote_pos = None
        self._row = 1
        self._col = 0
        self._record_row = None
        self._skip_lf = False    # \r seen, swallow a following \n

    def _error(self, message, recover=None):
        err = DelimParseError(message, self._row, self._col)
        if self.strict:
            raise err
        self.errors.append(err)
        if recover is not None:
            recover()

    def _start_record_if_needed(self):
        if self._record_row is None:
            self._record_row = self._row

    def _end_field(self):
        self._fields.append(''.join(self._field))
        self._field = []
        self._field_started = False
        self._after_quote = False

    def _end_record(self):
        if self._record_row is None:
            return
        self.records.append((self._record_row, self._fields))
        self._fields = []
        self._record_row = None

    def _newline(self, ch):
        if ch == '\r':
            self._skip_lf = True
        self._row += 1
        self._col = 0

    def feed(self, chunk):
        for ch in chunk:
            self._col += 1
            if self._skip_lf:
                self._skip_lf = False
                if ch == '\n':
                    self._col = 0
                    continue
            if self._in_quotes:
                if ch == self.quotechar:
                    self._in_quotes = False
                    self._after_quote = True
                else:
                    # Newlines inside quotes are data, not record ends.
                    self._field.append(ch)
                    if ch == '\n':
                        self._row += 1
                        self._col = 0
                continue
            if self._after_quote:
                if ch == self.quotechar:
                    # Doubled quote => escaped literal quote.
                    self._field.append(self.quotechar)
                    self._in_quotes = True
                    self._after_quote = False
                elif ch == self.delimiter:
                    self._end_field()
                elif ch == '\r' or ch == '\n':
                    self._end_field()
                    self._end_record()
                    self._newline(ch)
                elif ch == ' ' or ch == '\t':
                    pass  # tolerate padding between quote and delimiter
                else:
                    def _recover(ch=ch):
                        self._field.append(ch)
                    self._error("unexpected character %r after closing quote"
                                % ch, recover=_recover)
                    self._after_quote = False
                continue
            # Normal (unquoted) state.
            if ch == self.delimiter:
                self._start_record_if_needed()
                self._end_field()
            elif ch == '\r' or ch == '\n':
                if self._field_started or self._fields:
                    self._end_field()
                    self._end_record()
                # Completely blank lines are skipped (documented behavior).
                self._newline(ch)
            elif ch == self.quotechar and not self._field_started:
                self._start_record_if_needed()
                self._in_quotes = True
                self._field_started = True
                self._quote_pos = (self._row, self._col)
            else:
                # A quote char mid-field is literal data (mixed-quote safe).
                self._start_record_if_needed()
                self._field.append(ch)
                self._field_started = True

    def close(self):
        if self._in_quotes:
            row, col = self._quote_pos
            err = DelimParseError("unclosed quote", row, col)
            if self.strict:
                raise err
            self.errors.append(err)
            self.skipped += 1
            self._fields = []
            self._field = []
            self._field_started = False
            self._in_quotes = False
            self._record_row = None
        elif self._after_quote:
            self._end_field()
            self._end_record()
        elif self._field_started or self._fields:
            self._end_field()
            self._end_record()


class StreamParser:
    """Incremental parser. feed() any chunk sizes; results are identical
    to parsing the whole buffer at once."""

    def __init__(self, dialect, on_error='strict'):
        if on_error not in ('strict', 'skip'):
            raise ValueError("on_error must be 'strict' or 'skip'")
        self.dialect = dialect
        self.on_error = on_error
        self._scanner = _Scanner(dialect.delimiter, dialect.quotechar,
                                 strict=(on_error == 'strict'))
        self.expected_fields = None
        self.skipped = 0
        self.errors = []

    def feed(self, chunk):
        self._scanner.feed(chunk)
        return self._drain()

    def close(self):
        self._scanner.close()
        return self._drain()

    def _drain(self):
        accepted = []
        for row, fields in self._scanner.records:
            if self.expected_fields is None:
                self.expected_fields = len(fields)
            if len(fields) != self.expected_fields:
                err = DelimParseError(
                    "expected %d fields, got %d"
                    % (self.expected_fields, len(fields)), row, 1)
                if self.on_error == 'strict':
                    raise err
                self.errors.append(err)
                self.skipped += 1
                continue
            accepted.append(fields)
        self._scanner.records = []
        self.errors.extend(self._scanner.errors)
        self._scanner.errors = []
        self.skipped += self._scanner.skipped
        self._scanner.skipped = 0
        return accepted


class InferenceResult:
    def __init__(self, dialect, explanation, stats):
        self.dialect = dialect
        self.explanation = explanation
        self.stats = stats

    def __repr__(self):
        return "InferenceResult(%r)" % (self.dialect,)


class ParseResult:
    def __init__(self, records, skipped, errors, dialect, explanation):
        self.records = records
        self.skipped = skipped
        self.errors = errors
        self.dialect = dialect
        self.explanation = explanation


def _trial(text, delimiter, quotechar):
    """Parse `text` with a candidate dialect and collect statistics."""
    scanner = _Scanner(delimiter, quotechar, strict=True)
    error = None
    try:
        scanner.feed(text)
        scanner.close()
    except DelimParseError as exc:
        error = str(exc)
    counts = [len(fields) for _, fields in scanner.records]
    # Quote characters that were NOT interpreted as quoting but survive as
    # literal data at a field boundary are strong evidence that this trial
    # picked the wrong quote char (e.g. trying '\'' on a,"b,c",d).
    boundary_quotes = sum(
        1 for _, fields in scanner.records for f in fields
        if f and (f[0] in QUOTE_CANDIDATES or f[-1] in QUOTE_CANDIDATES))
    return {
        'delimiter': delimiter,
        'quotechar': quotechar,
        'rows': len(counts),
        'counts': counts,
        'error': error,
        'boundary_quotes': boundary_quotes,
    }


def _summarize(trial):
    d = repr(trial['delimiter'])
    q = repr(trial['quotechar'])
    if trial['error']:
        return "delimiter=%s quote=%s: parse error (%s)" % (d, q, trial['error'])
    if not trial['counts']:
        return "delimiter=%s quote=%s: no records" % (d, q)
    if len(set(trial['counts'])) > 1:
        return ("delimiter=%s quote=%s: inconsistent field counts %s"
                % (d, q, trial['counts']))
    return ("delimiter=%s quote=%s: %d row(s) x %d field(s), consistent"
            % (d, q, trial['rows'], trial['counts'][0]))


def infer_dialect(text, delimiters=DELIMITER_CANDIDATES,
                  quotechars=QUOTE_CANDIDATES):
    """Infer delimiter and quote char. Returns InferenceResult.

    Raises InferenceError when the evidence is insufficient or ambiguous;
    it never guesses. The same scoring path is used for any row count, so
    single-row and multi-row inputs cannot diverge.
    """
    if not text or not text.strip():
        raise InferenceError(
            "insufficient evidence: input is empty, cannot infer dialect")

    trials = [_trial(text, d, q) for q in quotechars for d in delimiters]
    summaries = [_summarize(t) for t in trials]

    valid = []
    for t in trials:
        consistent = len(set(t['counts'])) <= 1
        fields = t['counts'][0] if consistent and t['counts'] else 0
        t['fields'] = fields
        if t['error'] is None and t['rows'] > 0 and consistent and fields >= 2:
            valid.append(t)

    if not valid:
        raise InferenceError(
            "insufficient evidence: no candidate dialect produced a "
            "consistent multi-field parse. Candidates: " + "; ".join(summaries))

    min_boundary = min(t['boundary_quotes'] for t in valid)
    best = [t for t in valid if t['boundary_quotes'] == min_boundary]
    best_fields = max(t['fields'] for t in best)
    best = [t for t in best if t['fields'] == best_fields]
    best_rows = max(t['rows'] for t in best)
    best = [t for t in best if t['rows'] == best_rows]

    tied_delims = {t['delimiter'] for t in best}
    if len(tied_delims) > 1:
        raise InferenceError(
            "ambiguous evidence: delimiters %s all yield %d field(s) "
            "consistently; refusing to guess. Candidates: %s"
            % (sorted(repr(d) for d in tied_delims), best_fields,
               "; ".join(summaries)))

    # Same delimiter, possibly several quote chars: prefer the first
    # candidate quote char ('"' before "'").
    best.sort(key=lambda t: quotechars.index(t['quotechar']))
    chosen = best[0]
    dialect = Dialect(chosen['delimiter'], chosen['quotechar'])

    chosen_prefix = "delimiter=%r quote=%r:" % (dialect.delimiter,
                                                dialect.quotechar)
    explanation = (
        "chosen delimiter=%r quotechar=%r: %d row(s) x %d field(s), "
        "consistent across rows, no parse errors. Rejected candidates: %s"
        % (dialect.delimiter, dialect.quotechar, chosen['rows'],
           chosen['fields'],
           "; ".join(s for s in summaries
                     if not s.startswith(chosen_prefix))))
    return InferenceResult(dialect, explanation, trials)


def parse(text, dialect=None, on_error='strict'):
    """Parse whole text. Infers the dialect when none is given."""
    explanation = None
    if dialect is None:
        result = infer_dialect(text)
        dialect = result.dialect
        explanation = result.explanation
    stream = StreamParser(dialect, on_error=on_error)
    records = stream.feed(text)
    records += stream.close()
    return ParseResult(records, stream.skipped, stream.errors,
                       dialect, explanation)


if __name__ == '__main__':
    sample = ('name,age,note\n'
              'Alice,30,"likes, commas"\n'
              'Bob,25,"said ""hi"""\n'
              'Carol,41,"line one\nline two"\n')
    res = parse(sample)
    print("== inference rationale ==")
    print(res.explanation)
    print("== records ==")
    for row in res.records:
        print(row)
