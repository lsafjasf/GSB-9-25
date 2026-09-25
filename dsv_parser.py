"""dsv_parser.py —— 自动推断分隔符/引号规则的文本解析器（仅标准库）。

特性：
- 引号感知的状态机解析：字段内分隔符、引号内换行、双写转义引号均正确处理。
- 可解释的格式推断：infer() 返回 FormatSpec，含所选分隔符、引号字符与判定依据；
  统计证据不足（无一致多列结构 / 候选并列）时抛出 InferenceError，绝不瞎猜。
- 流式与整块解析完全一致：StreamParser.feed() 接受任意分块，结果与一次性解析逐字段相同。
- 非法输入明确处理：严格模式抛 ParseError（带行号/字段号）；
  宽松模式 on_error="skip" 跳过非法记录并计数，绝不静默丢行。
"""

from dataclasses import dataclass, field

CANDIDATE_DELIMITERS = [",", "\t", ";", "|", ":"]
CANDIDATE_QUOTES = ['"', "'"]
DEFAULT_QUOTE = '"'


class ParseError(Exception):
    """解析错误。row 为 1 起始的记录号，col 为 1 起始的字段号。"""

    def __init__(self, message, row=None, col=None):
        self.message = message
        self.row = row
        self.col = col
        loc = ""
        if row is not None:
            loc = " [row %d%s]" % (row, ", field %d" % col if col is not None else "")
        super().__init__(message + loc)


class InferenceError(Exception):
    """统计证据不足，无法可靠推断格式。"""


# ---------------------------------------------------------------- 核心状态机

class _Core:
    """增量式引号感知分词器。feed() 可喂任意大小的块，close() 收尾。"""

    START, UNQUOTED, QUOTED, QUOTE_AFTER = range(4)

    def __init__(self, delimiter, quotechar, strict_chars=True):
        self.delim = delimiter
        self.quote = quotechar
        self.strict_chars = strict_chars
        self.state = self.START
        self.buf = []            # 当前字段字符
        self.rec = []            # 当前记录已完成的字段
        self.records = []        # 已完成记录
        self.row_no = 1          # 正在构建的记录号（1 起始）
        self.field_no = 1        # 正在构建的字段号（1 起始）
        self.quote_open = None   # (row, field) 引号打开位置，用于报错
        self._skip_lf = False    # 刚处理了 \r，跳过紧随的 \n
        self._started = False    # 当前记录是否已有任何内容

    def _emit_field(self):
        self.rec.append("".join(self.buf))
        self.buf = []

    def _emit_record(self):
        self._emit_field()
        self.records.append(self.rec)
        self.rec = []
        self.row_no += 1
        self.field_no = 1
        self._started = False
        self.state = self.START

    def feed(self, chunk):
        for ch in chunk:
            if self._skip_lf:
                self._skip_lf = False
                if ch == "\n":
                    continue
            self._feed_char(ch)
        out, self.records = self.records, []
        return out

    def _feed_char(self, ch):
        st = self.state
        if st == self.QUOTED:
            if ch == self.quote:
                self.state = self.QUOTE_AFTER
            else:
                self.buf.append(ch)  # 引号内：分隔符/换行均为数据
            return
        if st == self.QUOTE_AFTER:
            if ch == self.quote:  # 双写引号 -> 转义的字面引号
                self.buf.append(self.quote)
                self.state = self.QUOTED
            elif ch == self.delim:
                self._emit_field()
                self.field_no += 1
                self.state = self.START
            elif ch == "\n":
                self._emit_record()
            elif ch == "\r":
                self._emit_record()
                self._skip_lf = True
            elif self.strict_chars:
                raise ParseError(
                    "unexpected character %r after closing quote" % ch,
                    self.row_no, self.field_no)
            else:  # 宽松：把多余字符并回字段
                self.buf.append(ch)
                self.state = self.UNQUOTED
            return
        # START / UNQUOTED
        if ch == self.delim:
            self._emit_field()
            self.field_no += 1
            self.state = self.START
            self._started = True
        elif ch == "\n":
            self._emit_record()
        elif ch == "\r":
            self._emit_record()
            self._skip_lf = True
        elif ch == self.quote and st == self.START:
            self.state = self.QUOTED
            self.quote_open = (self.row_no, self.field_no)
            self._started = True
        else:
            self.buf.append(ch)
            self.state = self.UNQUOTED
            self._started = True

    def close(self):
        if self.state == self.QUOTED:
            row, col = self.quote_open
            raise ParseError("unclosed quote", row, col)
        # 仅当确有未收尾内容时才产出最后一条记录（避免末尾换行产生幻影记录）
        if self.state == self.QUOTE_AFTER or self._started or self.rec or self.buf:
            self._emit_record()
        out, self.records = self.records, []
        return out


# ---------------------------------------------------------------- 格式推断

@dataclass
class CandidateStat:
    delimiter: str
    quotechar: str
    records: int
    fields: int
    consistent: bool
    stray_quotes: int = 0  # 字段内容中残留的引号字符数（越少越可信）


@dataclass
class FormatSpec:
    delimiter: str
    quotechar: str
    rationale: str           # 人类可读的判定依据
    stats: list              # 全部候选的 CandidateStat，便于审计


def _try_candidate(text, delim, quote, max_records):
    core = _Core(delim, quote, strict_chars=True)
    recs = []
    try:
        recs.extend(core.feed(text))
        recs.extend(core.close())
    except ParseError:
        recs.extend(core.records)
    return recs[:max_records]


def infer(text, max_records=100):
    """推断格式。证据不足时抛 InferenceError，绝不猜测。"""
    stats = []
    for q in CANDIDATE_QUOTES:
        for d in CANDIDATE_DELIMITERS:
            recs = _try_candidate(text, d, q, max_records)
            if not recs:
                stats.append(CandidateStat(d, q, 0, 0, False))
                continue
            counts = [len(r) for r in recs]
            consistent = len(set(counts)) == 1
            stray = sum(1 for r in recs for f in r
                        for qq in CANDIDATE_QUOTES if qq in f)
            stats.append(CandidateStat(
                d, q, len(recs), counts[0] if consistent else 0, consistent,
                stray))

    valid = [s for s in stats if s.consistent and s.fields >= 2]
    if not valid:
        raise InferenceError(
            "insufficient evidence: no (delimiter, quote) combination yields a "
            "consistent multi-column structure; pass an explicit FormatSpec to parse")

    best_records = max(s.records for s in valid)
    top = [s for s in valid if s.records == best_records]
    min_stray = min(s.stray_quotes for s in top)
    top = [s for s in top if s.stray_quotes == min_stray]
    best_fields = max(s.fields for s in top)
    top = [s for s in top if s.fields == best_fields]

    tied_delims = sorted({s.delimiter for s in top})
    if len(tied_delims) > 1:
        raise InferenceError(
            "ambiguous delimiter: %s all produce a consistent %d-column structure "
            "over %d record(s); pass an explicit FormatSpec to parse"
            % (["%r" % d for d in tied_delims], best_fields, best_records))

    notes = []
    if len(top) == 1:
        chosen = top[0]
    else:  # 同一分隔符、多个引号候选并列
        present = [s for s in top if s.quotechar in text]
        if len(present) == 1:
            chosen = present[0]
            notes.append("quote char %r appears in data, %r does not"
                         % (chosen.quotechar, [s.quotechar for s in top if s is not chosen]))
        elif not present:
            chosen = next(s for s in top if s.quotechar == DEFAULT_QUOTE)
            notes.append("no quote characters present in data; quotechar defaults to %r "
                         "(does not affect output)" % DEFAULT_QUOTE)
        else:
            raise InferenceError(
                "ambiguous quote char: %s both yield a consistent structure; "
                "pass an explicit FormatSpec to parse"
                % [s.quotechar for s in top])

    rationale = (
        "delimiter=%r, quotechar=%r: %d record(s) x %d field(s), fully consistent; "
        "%d of %d candidate (delimiter, quote) combinations were structurally valid"
        % (chosen.delimiter, chosen.quotechar, chosen.records, chosen.fields,
           len(valid), len(stats)))
    if notes:
        rationale += "; " + "; ".join(notes)
    return FormatSpec(chosen.delimiter, chosen.quotechar, rationale, stats)


# ---------------------------------------------------------------- 解析入口

@dataclass
class ParseResult:
    rows: list
    spec: FormatSpec = None
    skipped: int = 0                 # 宽松模式下被跳过的记录数
    errors: list = field(default_factory=list)  # 宽松模式下收集的 ParseError


class StreamParser:
    """流式解析器：feed() 接受任意分块，close() 收尾。结果与一次性解析一致。"""

    def __init__(self, spec, on_error="raise"):
        if on_error not in ("raise", "skip"):
            raise ValueError("on_error must be 'raise' or 'skip'")
        self.spec = spec
        self.on_error = on_error
        self._core = _Core(spec.delimiter, spec.quotechar,
                           strict_chars=(on_error == "raise"))
        self._expected = None
        self._row_no = 0
        self.rows = []
        self.errors = []
        self.skipped = 0

    def _accept(self, rec):
        self._row_no += 1
        if self._expected is None:
            self._expected = len(rec)
        if len(rec) != self._expected:
            err = ParseError(
                "field count mismatch: expected %d, got %d"
                % (self._expected, len(rec)), self._row_no)
            if self.on_error == "raise":
                raise err
            self.errors.append(err)
            self.skipped += 1
            return False
        self.rows.append(rec)
        return True

    def feed(self, chunk):
        return [rec for rec in self._core.feed(chunk) if self._accept(rec)]

    def close(self):
        try:
            recs = self._core.close()
        except ParseError as err:
            if self.on_error == "raise":
                raise
            self.errors.append(err)
            self.skipped += 1
            recs = []
        return [rec for rec in recs if self._accept(rec)]

    def result(self):
        return ParseResult(rows=self.rows, spec=self.spec,
                           skipped=self.skipped, errors=self.errors)


def parse(text, spec=None, on_error="raise"):
    """一次性解析。spec 为 None 时自动推断（证据不足抛 InferenceError）。"""
    if spec is None:
        spec = infer(text)
    p = StreamParser(spec, on_error)
    p.feed(text)
    p.close()
    return p.result()


def parse_chunks(chunks, spec=None, on_error="raise"):
    """流式解析。chunks 为任意大小的字符串序列，结果与 parse() 完全一致。"""
    chunks = list(chunks)
    if spec is None:
        spec = infer("".join(chunks))
    p = StreamParser(spec, on_error)
    for c in chunks:
        p.feed(c)
    p.close()
    return p.result()
