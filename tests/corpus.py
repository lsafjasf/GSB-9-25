"""Shared test corpus: normal / boundary / malformed cases + fuzz generators."""

# ---------------------------------------------------------------------------
# Hand-written corpus: (name, text).  Three families as required.
# ---------------------------------------------------------------------------

NORMAL = [
    ("single_cmp_int", "age >= 18"),
    ("single_cmp_str", "name = 'alice'"),
    ("single_cmp_dq_str", 'name = "alice"'),
    ("cmp_ref_value", "status = active"),
    ("and_chain", "a = 1 and b = 2 and c = 3"),
    ("or_chain", "a = 1 or b = 2 or c = 3"),
    ("and_binds_tighter", "a = 1 or b = 2 and c = 3"),
    ("parens_change_grouping", "(a = 1 or b = 2) and c = 3"),
    ("not_cmp", "not active = true"),
    ("not_group", "not (a = 1 or b = 2)"),
    ("double_not", "not not a = 1"),
    ("nested_parens", "((a = 1))"),
    ("float_value", "score > 3.25"),
    ("mixed_ops", "a != 1 and b < 2 and c <= 3 and d > 4 and e >= 5"),
    ("string_with_escape", "msg = 'it\\'s \\n ok'"),
    ("multiline_input", "a = 1 and\n  (b = 2 or\n   c = 3)"),
    ("keyword_like_idents", "android = 1 and orbit = 2 and nothing = 3"),
    ("empty_string_value", "note = ''"),
    ("underscore_ident", "_tmp_1 = 0"),
]

BOUNDARY = [
    ("empty", ""),
    ("whitespace_only", "   \t\r\n  "),
    ("no_spaces_anywhere", "a=1"),
    ("no_spaces_parens", "(a=1)and(b=2)"),
    ("zero_value", "n = 0"),
    ("zero_float", "n = 0.0"),
    ("big_int", "n = 123456789012345678901234567890"),
    ("leading_float_zero", "n = 0.5"),
    ("deep_nesting_30", "(" * 30 + "x = 1" + ")" * 30),
    ("long_and_chain_50", " and ".join("f%d = %d" % (k, k) for k in range(50))),
    ("long_or_chain_50", " or ".join("f%d = %d" % (k, k) for k in range(50))),
    ("not_chain_20", "not " * 20 + "x = 1"),
    ("ident_with_digits", "a1b2_c3 = 7"),
    ("string_all_escapes", r"s = '\\\"\n\t\''"),
    ("string_with_spaces", "s = '  spaced out  '"),
    ("string_with_unicode", "s = 'héllo→世界'"),
    ("op_at_eof_after_value", "a = 1 "),
    ("tabs_and_crlf", "a\t=\t1\r\nand\r\nb\t=\t2"),
]

MALFORMED = [
    ("lone_keyword_and", "and"),
    ("lone_keyword_not", "not"),
    ("not_not_eof", "not not"),
    ("lone_ident", "age"),
    ("ident_then_number", "age 18"),
    ("missing_value", "age ="),
    ("keyword_as_value", "age = and"),
    ("keyword_as_field", "and = 1"),
    ("unclosed_paren", "(age = 1"),
    ("unopened_paren", "age = 1)"),
    ("empty_parens", "()"),
    ("paren_then_eof", "("),
    ("double_op", "age == 1"),
    ("op_without_field", "= 1"),
    ("trailing_tokens", "age = 1 2"),
    ("trailing_ident", "age = 1 name"),
    ("value_then_op", "age = 1 >"),
    ("unexpected_char_at", "@"),
    ("unexpected_char_dot", "a = 1.2.3"),
    ("unexpected_char_dash", "n = -1"),
    ("unexpected_char_unicode", "年龄 = 1"),
    ("unterminated_string", "x = 'abc"),
    ("unterminated_string_escape_eof", "x = 'abc\\"),
    ("invalid_escape", "x = 'a\\qb'"),
    ("malformed_number_dot", "12."),
    ("malformed_number_ident", "1abc"),
    ("number_then_dot_ident", "1.a"),
    ("string_then_string", "a = 'x' 'y'"),
    ("nested_unclosed", "((a = 1)"),
    ("not_then_rparen", "(not)"),
]

CASES = NORMAL + BOUNDARY + MALFORMED

# ---------------------------------------------------------------------------
# Fuzz generators (seeded, deterministic).
# ---------------------------------------------------------------------------

_FIELDS = ["age", "name", "x", "status_code", "a1", "_tmp", "anderson"]
_OPS = ["=", "!=", ">", ">=", "<", "<="]


def gen_valid(rng, max_depth=4):
    """Generate a syntactically valid expression."""
    def expr(depth):
        if depth <= 0:
            return cmp_()
        r = rng.random()
        if r < 0.35:
            return cmp_()
        if r < 0.55:
            return "(" + expr(depth - 1) + ")"
        if r < 0.7:
            return "not " + expr(depth - 1)
        joiner = " and " if rng.random() < 0.5 else " or "
        return expr(depth - 1) + joiner + expr(depth - 1)

    def cmp_():
        return rng.choice(_FIELDS) + " " + rng.choice(_OPS) + " " + value()

    def value():
        r = rng.random()
        if r < 0.35:
            return str(rng.randint(0, 10 ** rng.randint(1, 6)))
        if r < 0.5:
            return "%d.%d" % (rng.randint(0, 999), rng.randint(0, 999))
        if r < 0.8:
            body = "".join(rng.choice("abc XYZ019 ._-") for _ in range(rng.randint(0, 8)))
            return "'" + body + "'"
        return rng.choice(_FIELDS)

    return expr(max_depth)


def gen_soup(rng, max_len=40):
    """Generate arbitrary token soup, mostly malformed."""
    alphabet = (
        "aabbx_ 0123456789.=<>!()'\"\\ \t\n"
        "and or not anderson "
    )
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, max_len)))


def gen_mutation(rng, max_depth=3):
    """Take a valid expression and corrupt one random character."""
    text = gen_valid(rng, max_depth)
    if not text:
        return text
    pos = rng.randrange(len(text) + 1)
    op = rng.random()
    if op < 0.4 and text:
        pos = rng.randrange(len(text))
        return text[:pos] + text[pos + 1:]          # delete a char
    if op < 0.8:
        ch = rng.choice("=()'\\@.an ")
        return text[:pos] + ch + text[pos:]          # insert a char
    if text:
        pos = rng.randrange(len(text))
        return text[:pos] + rng.choice("=()'\\@.x ") + text[pos + 1:]
    return text
