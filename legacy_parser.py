"""Legacy monolithic filter-expression parser (pre-refactor baseline).

Everything lives inside ``parse``: character scanning, token production,
recursive-descent structure judgement, result-dict assembly and error
construction.  It works, but none of the intermediate steps can be exercised
independently and any local change tends to ripple through the whole
function.

Language (a small boolean filter DSL)::

    expr     := or_expr
    or_expr  := and_expr (OR and_expr)*
    and_expr := not_expr (AND not_expr)*
    not_expr := NOT not_expr | atom
    atom     := LPAREN or_expr RPAREN | IDENT OP value
    value    := NUMBER | STRING | IDENT

Return contract::

    {"ok": True,  "ast": <assembled dict>}
    {"ok": False, "error": {"category": "lex"|"syntax",
                            "message": str,
                            "pos": int, "line": int, "col": int}}
"""

_DIGITS = "0123456789"
_WS = " \t\r\n"
_KEYWORDS = ("and", "or", "not")
_TWO_CHAR_OPS = ("!=", ">=", "<=")
_ONE_CHAR_OPS = "=><"
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}


def _is_ident_start(ch):
    return ch == "_" or ("a" <= ch <= "z") or ("A" <= ch <= "Z")


def _is_ident_part(ch):
    return _is_ident_start(ch) or ("0" <= ch <= "9")


class _Err(Exception):
    def __init__(self, category, message, pos):
        super().__init__(message)
        self.category = category
        self.message = message
        self.pos = pos


def _fail(text, exc):
    line = text.count("\n", 0, exc.pos) + 1
    col = exc.pos - text.rfind("\n", 0, exc.pos)
    return {
        "ok": False,
        "error": {
            "category": exc.category,
            "message": exc.message,
            "pos": exc.pos,
            "line": line,
            "col": col,
        },
    }


def parse(text):
    n = len(text)

    # ------------------------------------------------------------------
    # Stage 1 (inline): tokenize the whole text up front.
    # ------------------------------------------------------------------
    raw_tokens = []
    i = 0
    try:
        while i < n:
            ch = text[i]
            if ch in _WS:
                i += 1
                continue

            # identifier / keyword
            if _is_ident_start(ch):
                start = i
                while i < n and _is_ident_part(text[i]):
                    i += 1
                word = text[start:i]
                if word in _KEYWORDS:
                    raw_tokens.append((word.upper(), None, start))
                else:
                    raw_tokens.append(("IDENT", word, start))
                continue

            # number: int or fractional float, dot must be followed by digit
            if ch in _DIGITS:
                start = i
                while i < n and text[i] in _DIGITS:
                    i += 1
                is_float = False
                if i < n and text[i] == ".":
                    if i + 1 < n and text[i + 1] in _DIGITS:
                        is_float = True
                        i += 1
                        while i < n and text[i] in _DIGITS:
                            i += 1
                    else:
                        raise _Err("lex", "malformed number literal", i)
                if i < n and _is_ident_part(text[i]):
                    raise _Err("lex", "malformed number literal", start)
                raw = text[start:i]
                raw_tokens.append(
                    ("NUMBER", float(raw) if is_float else int(raw), start)
                )
                continue

            # string literal with a tiny escape table
            if ch == "'" or ch == '"':
                start = i
                quote = ch
                i += 1
                buf = []
                closed = False
                while i < n:
                    c = text[i]
                    if c == "\\":
                        if i + 1 >= n:
                            raise _Err("lex", "unterminated string literal", start)
                        esc = text[i + 1]
                        if esc not in _ESCAPES:
                            raise _Err(
                                "lex", "invalid escape sequence '\\" + esc + "'", i
                            )
                        buf.append(_ESCAPES[esc])
                        i += 2
                        continue
                    if c == quote:
                        closed = True
                        i += 1
                        break
                    buf.append(c)
                    i += 1
                if not closed:
                    raise _Err("lex", "unterminated string literal", start)
                raw_tokens.append(("STRING", "".join(buf), start))
                continue

            # operators / punctuation
            two = text[i:i + 2]
            if two in _TWO_CHAR_OPS:
                raw_tokens.append(("OP", two, i))
                i += 2
                continue
            if ch in _ONE_CHAR_OPS:
                raw_tokens.append(("OP", ch, i))
                i += 1
                continue
            if ch == "(":
                raw_tokens.append(("LPAREN", None, i))
                i += 1
                continue
            if ch == ")":
                raw_tokens.append(("RPAREN", None, i))
                i += 1
                continue

            raise _Err("lex", "unexpected character %r" % ch, i)
    except _Err as exc:
        return _fail(text, exc)

    raw_tokens.append(("EOF", None, n))

    # ------------------------------------------------------------------
    # Stage 2 + 3 (inline): recursive descent + dict assembly.
    # ------------------------------------------------------------------
    ti = [0]

    def peek():
        return raw_tokens[ti[0]]

    def advance():
        tok = raw_tokens[ti[0]]
        ti[0] += 1
        return tok

    def parse_or():
        left = parse_and()
        while peek()[0] == "OR":
            advance()
            right = parse_and()
            left = {"type": "or", "left": left, "right": right}
        return left

    def parse_and():
        left = parse_not()
        while peek()[0] == "AND":
            advance()
            right = parse_not()
            left = {"type": "and", "left": left, "right": right}
        return left

    def parse_not():
        if peek()[0] == "NOT":
            advance()
            return {"type": "not", "operand": parse_not()}
        return parse_atom()

    def parse_atom():
        kind, value, pos = peek()
        if kind == "LPAREN":
            advance()
            inner = parse_or()
            if peek()[0] != "RPAREN":
                raise _Err("syntax", "expected ')'", peek()[2])
            advance()
            return inner
        if kind == "IDENT":
            advance()
            op_kind, op_value, op_pos = peek()
            if op_kind != "OP":
                raise _Err("syntax", "expected comparison operator", op_pos)
            advance()
            val_kind, val_value, val_pos = peek()
            if val_kind == "NUMBER":
                advance()
                assembled_value = val_value
            elif val_kind == "STRING":
                advance()
                assembled_value = val_value
            elif val_kind == "IDENT":
                advance()
                assembled_value = {"type": "ref", "name": val_value}
            else:
                raise _Err("syntax", "expected value", val_pos)
            return {
                "type": "cmp",
                "field": value,
                "op": op_value,
                "value": assembled_value,
            }
        raise _Err("syntax", "expected operand", pos)

    # ------------------------------------------------------------------
    # Stage 4 (inline): turn internal failures into the public error dict.
    # ------------------------------------------------------------------
    try:
        ast = parse_or()
        if peek()[0] != "EOF":
            raise _Err("syntax", "unexpected token", peek()[2])
        return {"ok": True, "ast": ast}
    except _Err as exc:
        return _fail(text, exc)
