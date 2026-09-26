"""The ORIGINAL monolithic parser, kept for differential regression tests.

This is deliberately a single, long function that mixes tokenisation, grammar
checking, result assembly and error reporting (with ad-hoc line/column math at
every raise site).  It represents the pre-refactoring baseline and must not be
"improved": refactored/monolith equivalence is enforced by
``tests/test_differential.py``.
"""

from __future__ import annotations


def parse_query(source):
    # ---- error helper (message + position computed inline, like old code)
    def fail(code, offset, message):
        line = source.count("\n", 0, offset) + 1
        last_nl = source.rfind("\n", 0, offset)
        column = offset + 1 if last_nl < 0 else offset - last_nl
        return {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "offset": offset,
                "line": line,
                "column": column,
            },
        }

    keywords = ("SELECT", "FROM", "WHERE", "LIMIT", "AND", "OR")
    digits = "0123456789"
    spaces = " \t\r\n"
    id_start = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
    id_body = id_start + digits

    # ================= Phase 1: tokenisation, inlined ===================
    tokens = []
    n = len(source)
    i = 0
    while i < n:
        ch = source[i]
        if ch in spaces:
            i += 1
            continue
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            i += 2
            while i < n and source[i] != "\n":
                i += 1
            continue
        start = i
        if ch == '"':
            buf = []
            j = i + 1
            while j < n and source[j] != '"':
                c = source[j]
                if c == "\n":
                    return fail("E_UNCLOSED_STRING", start,
                                "unterminated string literal")
                if c == "\\":
                    if j + 1 >= n or source[j + 1] == "\n":
                        return fail("E_BAD_ESCAPE",
                                    j + 1 if j + 1 < n else j,
                                    "invalid escape sequence \\")
                    esc = source[j + 1]
                    mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
                    if esc not in mapping:
                        return fail("E_BAD_ESCAPE", j + 1,
                                    "invalid escape sequence \\" + esc)
                    buf.append(mapping[esc])
                    j += 2
                else:
                    buf.append(c)
                    j += 1
            if j >= n:
                return fail("E_UNCLOSED_STRING", start,
                            "unterminated string literal")
            tokens.append(("STRING", "".join(buf), start,
                           source[start:j + 1]))
            i = j + 1
            continue
        if ch in digits:
            j = i + 1
            while j < n and source[j] in digits:
                j += 1
            tokens.append(("NUMBER", int(source[i:j]), start,
                           source[i:j]))
            i = j
            continue
        if ch in id_start:
            j = i + 1
            while j < n and source[j] in id_body:
                j += 1
            word = source[i:j]
            up = word.upper()
            if up in keywords:
                tokens.append(("KEYWORD", up, start, word))
            else:
                tokens.append(("IDENT", word, start, word))
            i = j
            continue
        two = source[i:i + 2]
        if two in ("<=", ">=", "<>", "!="):
            tokens.append(("OP", two, start, two))
            i += 2
            continue
        if ch in "<>":
            tokens.append(("OP", ch, start, ch))
            i += 1
            continue
        if ch in "(),=-":
            tokens.append(("OP", ch, start, ch))
            i += 1
            continue
        return fail("E_UNEXPECTED_CHAR", start,
                    "unexpected character " + repr(ch))

    # ================= token cursor helpers =============================
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def advance():
        tok = tokens[pos[0]]
        pos[0] += 1
        return tok

    def cur_offset():
        tok = peek()
        return n if tok is None else tok[2]

    def describe(tok):
        if tok is None:
            return "end of query"
        if tok[0] == "KEYWORD":
            return tok[1]
        if tok[0] == "STRING":
            return tok[3]
        if tok[0] == "NUMBER":
            return str(tok[1])
        return tok[1]

    def fail_expected(expected):
        tok = peek()
        return fail("E_UNEXPECTED_TOKEN", cur_offset(),
                    "expected " + expected + " but found " + describe(tok))

    def is_keyword(tok, word):
        return tok is not None and tok[0] == "KEYWORD" and tok[1] == word

    def take(word):
        if is_keyword(peek(), word):
            advance()
            return True
        return False

    # ================= Phase 2: grammar, inlined ========================
    def parse_atom():
        tok = peek()
        if tok is None:
            return fail_expected("an expression")
        if tok[0] in ("NUMBER", "STRING"):
            advance()
            return ("lit", tok[1], tok[2])
        if tok[0] == "IDENT":
            advance()
            return ("ref", tok[1], tok[2])
        if tok[0] == "OP" and tok[1] == "(":
            advance()
            expr = parse_or()
            if isinstance(expr, dict):
                return expr
            close = peek()
            if close is None or not (close[0] == "OP" and close[1] == ")"):
                return fail("E_UNCLOSED_GROUP", cur_offset(),
                            "unclosed parenthesised group")
            advance()
            return expr
        return fail_expected("an expression")

    def parse_compare():
        left = parse_atom()
        if isinstance(left, dict):
            return left
        tok = peek()
        if tok is None or not (tok[0] == "OP"
                               and tok[1] in ("=", "<>", "!=", "<", "<=",
                                              ">", ">=")):
            return left
        advance()
        op_off = tok[2]
        right = parse_atom()
        if isinstance(right, dict):
            return right
        chained = peek()
        if chained is not None and chained[0] == "OP" and chained[1] in (
                "=", "<>", "!=", "<", "<=", ">", ">="):
            return fail("E_UNEXPECTED_TOKEN", chained[2],
                        "expected AND, OR or end of expression but found "
                        + describe(chained))
        return ("cmp", tok[1], left, right, op_off)

    def parse_and():
        left = parse_compare()
        if isinstance(left, dict):
            return left
        while take("AND"):
            op_off = tokens[pos[0] - 1][2]
            right = parse_compare()
            if isinstance(right, dict):
                return right
            left = ("and", left, right, op_off)
        return left

    def parse_or():
        left = parse_and()
        if isinstance(left, dict):
            return left
        while take("OR"):
            op_off = tokens[pos[0] - 1][2]
            right = parse_and()
            if isinstance(right, dict):
                return right
            left = ("or", left, right, op_off)
        return left

    if not take("SELECT"):
        return fail_expected("SELECT")

    columns = []
    first = peek()
    if first is None or first[0] != "IDENT":
        return fail_expected("a column name")
    advance()
    columns.append((first[1], first[2]))
    while True:
        tok = peek()
        if tok is not None and tok[0] == "OP" and tok[1] == ",":
            advance()
            nxt = peek()
            if nxt is None or nxt[0] != "IDENT":
                return fail_expected("a column name")
            advance()
            columns.append((nxt[1], nxt[2]))
            continue
        if tok is not None and tok[0] == "IDENT":
            return fail_expected("','")
        break

    if not take("FROM"):
        return fail_expected("FROM")
    tbl = peek()
    if tbl is None or tbl[0] != "IDENT":
        return fail_expected("a table name")
    advance()

    where_expr = None
    limit_val = None
    seen_where = False
    seen_limit = False
    while True:
        nxt = peek()
        if nxt is None:
            break
        if is_keyword(nxt, "WHERE"):
            if seen_limit:
                return fail_expected("end of query")
            if seen_where:
                return fail_expected("LIMIT or end of query")
            advance()
            seen_where = True
            where_expr = parse_or()
            if isinstance(where_expr, dict):
                return where_expr
        elif is_keyword(nxt, "LIMIT"):
            if seen_limit:
                return fail_expected("end of query")
            advance()
            seen_limit = True
            negate = False
            t = peek()
            if t is not None and t[0] == "OP" and t[1] == "-":
                advance()
                negate = True
            t = peek()
            if t is None or t[0] != "NUMBER":
                return fail_expected("a number")
            advance()
            limit_val = (-t[1] if negate else t[1], t[2])
        else:
            return fail_expected("LIMIT or end of query")

    # ================= Phase 3: assembly, inlined =======================
    select_names = [c[0] for c in columns]
    seen_cols = set()
    for name, off in columns:
        if name in seen_cols:
            return fail("E_DUPLICATE_COLUMN", off,
                        "duplicate column " + repr(name))
        seen_cols.add(name)

    def eoff(expr):
        tag = expr[0]
        if tag in ("ref", "lit"):
            return expr[2]
        if tag == "cmp":
            return expr[4]
        return expr[3]

    def references(expr):
        tag = expr[0]
        if tag == "ref":
            return True
        if tag == "lit":
            return False
        if tag == "cmp":
            return references(expr[2]) or references(expr[3])
        return references(expr[1]) or references(expr[2])

    def emit(expr):
        tag = expr[0]
        if tag == "ref":
            return {"type": "ref", "name": expr[1]}
        if tag == "lit":
            return {"type": "literal", "value": expr[1]}
        if tag == "cmp":
            return {"type": "cmp", "op": expr[1],
                    "left": emit(expr[2]), "right": emit(expr[3])}
        op = "and" if tag == "and" else "or"
        args = []
        for child in (expr[1], expr[2]):
            emitted = emit(child)
            if emitted.get("type") == op:
                args.extend(emitted["args"])
            else:
                args.append(emitted)
        return {"type": op, "args": args}

    where_out = None
    if where_expr is not None:
        if not references(where_expr):
            return fail("E_WHERE_CONSTANT", eoff(where_expr),
                        "WHERE condition must reference at least one column")
        where_out = emit(where_expr)

    limit_out = None
    if limit_val is not None:
        value, off = limit_val
        if not 0 <= value <= 1000:
            return fail("E_LIMIT_RANGE", off,
                        "LIMIT must be between 0 and 1000, got " + str(value))
        limit_out = value

    return {
        "ok": True,
        "query": {
            "select": select_names,
            "from": tbl[1],
            "where": where_out,
            "limit": limit_out,
        },
    }
