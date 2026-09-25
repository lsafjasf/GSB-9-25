"""Plural-rule engine driven entirely by language configuration.

Each locale declares an ordered list of categories. A category has a name
and a `when` expression over the quantity `n`; the category whose `when`
is null is the catch-all (and must be named "other"). Expressions support:
    n, integer/decimal literals, + - * / %, parentheses,
    = != < <= > >=, ranges `a..b` with `in`, and/or/not
Evaluation returns a trace so every classification is explainable.
"""
from __future__ import annotations

import re

from .errors import ConfigError, RenderError

_TOKEN_RE = re.compile(r"""
      (?P<num>\d+(?:\.\d+)?)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op>!=|<=|>=|\.\.|=|<|>|%|\+|-|\*|/|\(|\))
    | (?P<ws>\s+)
    | (?P<bad>.)
""", re.VERBOSE)

_KEYWORDS = {"and", "or", "not", "in", "n"}


def _tokenize(src: str):
    tokens = []
    for m in _TOKEN_RE.finditer(src):
        kind = m.lastgroup
        text = m.group()
        if kind == "ws":
            continue
        if kind == "bad":
            raise ConfigError(f"plural rule {src!r}: unexpected character "
                              f"{text!r} at offset {m.start()}")
        if kind == "num":
            tokens.append(("num", float(text) if "." in text else int(text)))
        elif kind == "ident" and text not in _KEYWORDS:
            raise ConfigError(f"plural rule {src!r}: unknown identifier "
                              f"{text!r} (only 'n' is allowed)")
        elif kind == "ident":
            tokens.append((text, None))
        else:
            tokens.append((text, None))
    tokens.append(("end", None))
    return tokens


class _Parser:
    def __init__(self, tokens, src):
        self.tokens = tokens
        self.i = 0
        self.src = src

    def peek(self):
        return self.tokens[self.i][0]

    def next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, kind):
        if self.peek() != kind:
            raise ConfigError(f"plural rule {self.src!r}: expected {kind!r}, "
                              f"got {self.peek()!r}")
        return self.next()

    def parse(self):
        node = self.parse_or()
        if self.peek() != "end":
            raise ConfigError(f"plural rule {self.src!r}: trailing tokens")
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.peek() == "or":
            self.next()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_unary()
        while self.peek() == "and":
            self.next()
            node = ("and", node, self.parse_unary())
        return node

    def parse_unary(self):
        if self.peek() == "not":
            self.next()
            return ("not", self.parse_unary())
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_arith()
        tok = self.peek()
        if tok in ("=", "!=", "<", "<=", ">", ">="):
            self.next()
            return ("cmp", tok, left, self.parse_arith())
        if tok == "in":
            self.next()
            lo = self.parse_arith()
            self.expect("..")
            hi = self.parse_arith()
            return ("in", left, lo, hi)
        return left

    def parse_arith(self):
        node = self.parse_term()
        while self.peek() in ("+", "-"):
            op = self.next()[0]
            node = ("bin", op, node, self.parse_term())
        return node

    def parse_term(self):
        node = self.parse_factor()
        while self.peek() in ("*", "/", "%"):
            op = self.next()[0]
            node = ("bin", op, node, self.parse_factor())
        return node

    def parse_factor(self):
        tok, val = self.next()
        if tok == "num":
            return ("num", val)
        if tok == "n":
            return ("n",)
        if tok == "-":
            return ("neg", self.parse_factor())
        if tok == "(":
            node = self.parse_or()
            self.expect(")")
            return node
        raise ConfigError(f"plural rule {self.src!r}: unexpected {tok!r}")


def compile_expression(src: str):
    """Compile a rule expression to an AST. Raises ConfigError if invalid."""
    return _Parser(_tokenize(src), src).parse()


def _eval_node(node, n):
    op = node[0]
    if op == "num":
        return node[1]
    if op == "n":
        return n
    if op == "neg":
        return -_eval_node(node[1], n)
    if op == "bin":
        a = _eval_node(node[2], n)
        b = _eval_node(node[3], n)
        sym = node[1]
        if sym == "+":
            return a + b
        if sym == "-":
            return a - b
        if sym == "*":
            return a * b
        if sym == "/":
            if b == 0:
                raise RenderError("plural rule: division by zero")
            return a / b
        if sym == "%":
            if b == 0:
                raise RenderError("plural rule: modulo by zero")
            return a % b
        raise AssertionError(sym)
    if op == "cmp":
        a = _eval_node(node[2], n)
        b = _eval_node(node[3], n)
        sym = node[1]
        return {"=": a == b, "!=": a != b, "<": a < b, "<=": a <= b,
                ">": a > b, ">=": a >= b}[sym]
    if op == "in":
        v = _eval_node(node[1], n)
        return _eval_node(node[2], n) <= v <= _eval_node(node[3], n)
    if op == "and":
        return bool(_eval_node(node[1], n)) and bool(_eval_node(node[2], n))
    if op == "or":
        return bool(_eval_node(node[1], n)) or bool(_eval_node(node[2], n))
    if op == "not":
        return not _eval_node(node[1], n)
    raise AssertionError(f"bad node {node!r}")


def select_plural(compiled_categories, n):
    """Pick the plural category for quantity `n`.

    `compiled_categories` is a list of (name, ast_or_None, source_or_None).
    Returns (category_name, evaluations) where evaluations is a list of
    dicts {"category", "when", "result"} making the decision explainable.
    """
    evaluations = []
    default = None
    for name, ast, src in compiled_categories:
        if ast is None:
            default = name
            evaluations.append({"category": name, "when": None,
                                "result": "default"})
            continue
        result = bool(_eval_node(ast, n))
        evaluations.append({"category": name, "when": src, "result": result})
        if result:
            return name, evaluations
    if default is None:
        raise RenderError("plural rules have no catch-all category")
    return default, evaluations
