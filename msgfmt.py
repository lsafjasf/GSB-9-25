"""msgfmt — 按语言配置渲染界面文案的消息格式化库（仅标准库）。

能力：
- 具名占位符 {name}，顺序由模板决定，可任意调整；
  缺失 / 多余 / 重复使用的占位符都能检测并报出名称与位置。
- 复数分档：{count, plural, one{...} other{...}}，规则由语言配置声明，
  支持整数、零与小数，分档结果带可解释说明。
- 数字与日期本地化：千分位、小数位、日期顺序全部来自语言配置。
- 回退链：消息缺失时按语言配置声明的顺序回退，结果标注实际使用的语言。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field


class ConfigError(Exception):
    """语言配置非法。"""


class MessageFormatError(Exception):
    """模板语法错误或参数与模板不匹配。"""


class MissingMessageError(KeyError):
    """整条回退链都找不到该消息。"""


# ---------------------------------------------------------------- 模板 AST

@dataclass
class Text:
    value: str


@dataclass
class Placeholder:
    name: str
    pos: int  # 在模板字符串中的字符位置


@dataclass
class NumberRef:
    pos: int  # '#'，仅在 plural 分支内合法，指代当前复数数量


@dataclass
class Plural:
    name: str
    pos: int
    options: dict  # category -> [nodes]


_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


def _read_ident(text, i, what):
    start = i
    while i < len(text) and text[i] in _IDENT_CHARS:
        i += 1
    if i == start:
        raise MessageFormatError("expected %s at position %d in template %r" % (what, start, text))
    return text[start:i], i


def _skip_spaces(text, i):
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def parse_template(text):
    """把模板字符串解析为节点列表。"""
    nodes, i = _parse_nodes(text, 0, stop_at_brace=False)
    return nodes


def _parse_nodes(text, i, stop_at_brace):
    nodes = []
    buf = []
    while i < len(text):
        c = text[i]
        if c == "{":
            if buf:
                nodes.append(Text("".join(buf)))
                buf = []
            node, i = _parse_placeholder(text, i)
            nodes.append(node)
        elif c == "}":
            if not stop_at_brace:
                raise MessageFormatError("unmatched '}' at position %d in template %r" % (i, text))
            if buf:
                nodes.append(Text("".join(buf)))
            return nodes, i + 1
        elif c == "#" and stop_at_brace:
            if buf:
                nodes.append(Text("".join(buf)))
                buf = []
            nodes.append(NumberRef(i))
            i += 1
        else:
            buf.append(c)
            i += 1
    if stop_at_brace:
        raise MessageFormatError("unclosed '{' in template %r" % text)
    if buf:
        nodes.append(Text("".join(buf)))
    return nodes, i


def _parse_placeholder(text, i):
    pos = i
    i += 1  # 跳过 '{'
    i = _skip_spaces(text, i)
    name, i = _read_ident(text, i, "placeholder name")
    i = _skip_spaces(text, i)
    if i >= len(text):
        raise MessageFormatError("unclosed '{' at position %d in template %r" % (pos, text))
    if text[i] == "}":
        return Placeholder(name, pos), i + 1
    if text[i] != ",":
        raise MessageFormatError("unexpected %r at position %d in template %r" % (text[i], i, text))
    i = _skip_spaces(text, i + 1)
    kind, i = _read_ident(text, i, "placeholder kind")
    if kind != "plural":
        raise MessageFormatError("unknown placeholder kind %r at position %d" % (kind, pos))
    i = _skip_spaces(text, i)
    if i >= len(text) or text[i] != ",":
        raise MessageFormatError("expected ',' after 'plural' at position %d" % i)
    i = _skip_spaces(text, i + 1)
    options = {}
    while True:
        i = _skip_spaces(text, i)
        if i >= len(text):
            raise MessageFormatError("unclosed plural at position %d in template %r" % (pos, text))
        if text[i] == "}":
            if not options:
                raise MessageFormatError("plural at position %d has no options" % pos)
            return Plural(name, pos, options), i + 1
        cat, i = _read_ident(text, i, "plural category")
        i = _skip_spaces(text, i)
        if i >= len(text) or text[i] != "{":
            raise MessageFormatError("expected '{' after category %r at position %d" % (cat, i))
        if cat in options:
            raise MessageFormatError("duplicate plural category %r at position %d" % (cat, pos))
        sub, i = _parse_nodes(text, i + 1, stop_at_brace=True)
        options[cat] = sub


def collect_placeholders(nodes, into=None):
    """按出现顺序收集 (name, pos)，含重复。"""
    if into is None:
        into = []
    for node in nodes:
        if isinstance(node, Placeholder):
            into.append((node.name, node.pos))
        elif isinstance(node, Plural):
            into.append((node.name, node.pos))
            for sub in node.options.values():
                collect_placeholders(sub, into)
    return into


# ---------------------------------------------------------------- 复数规则

@dataclass
class PluralDecision:
    n: object
    category: str
    rule_index: int
    explanation: str


_KNOWN_OPS = {"always", "eq", "is_integer", "not_integer", "mod_eq", "mod_in", "mod_not_in"}


def _check_condition(cond, n):
    op = cond["op"]
    if op == "always":
        return True
    if op == "eq":
        return n == cond["value"]
    if op == "is_integer":
        return float(n).is_integer()
    if op == "not_integer":
        return not float(n).is_integer()
    if op == "mod_eq":
        return n % cond["mod"] == cond["value"]
    if op == "mod_in":
        return n % cond["mod"] in cond["values"]
    if op == "mod_not_in":
        return n % cond["mod"] not in cond["values"]
    raise ConfigError("unknown plural op %r" % op)


def _describe_condition(cond):
    op = cond["op"]
    if op == "always":
        return "always"
    if op == "eq":
        return "n == %s" % cond["value"]
    if op == "is_integer":
        return "n is integer"
    if op == "not_integer":
        return "n is not integer"
    if op == "mod_eq":
        return "n %% %s == %s" % (cond["mod"], cond["value"])
    if op == "mod_in":
        return "n %% %s in %s" % (cond["mod"], cond["values"])
    if op == "mod_not_in":
        return "n %% %s not in %s" % (cond["mod"], cond["values"])
    return op


def decide_plural(n, rules):
    """按声明顺序评估规则，返回第一个命中的分档（可解释）。"""
    for idx, rule in enumerate(rules):
        if all(_check_condition(c, n) for c in rule["when"]):
            why = " and ".join(_describe_condition(c) for c in rule["when"])
            return PluralDecision(n, rule["category"], idx,
                                  "n=%s matches rule #%d (%s) -> %s"
                                  % (n, idx, why, rule["category"]))
    raise ConfigError("no plural rule matched n=%r (config must end with a catch-all)"
                      % (n,))


# ---------------------------------------------------------------- 数字与日期

def format_number(value, cfg):
    """按语言配置格式化数字：千分位、小数位、小数点符号。"""
    group = cfg["group"]
    ts = cfg["thousands_sep"]
    ds = cfg["decimal_sep"]
    maxd = cfg["max_decimals"]
    trim = cfg.get("trim_zeros", True)
    neg = float(value) < 0
    if isinstance(value, int) or float(value).is_integer():
        intpart = str(abs(int(value)))
        frac = ""
    else:
        s = "%.*f" % (maxd, abs(float(value)))
        intpart, frac = s.split(".")
        if trim:
            frac = frac.rstrip("0")
    chunks = []
    while len(intpart) > group:
        chunks.append(intpart[-group:])
        intpart = intpart[:-group]
    chunks.append(intpart)
    out = ts.join(reversed(chunks))
    if frac:
        out += ds + frac
    return ("-" if neg else "") + out


def format_date(value, cfg):
    """按语言配置格式化日期：顺序、分隔符、是否补零。value 为 date/datetime/(y,m,d)。"""
    if isinstance(value, (datetime.date, datetime.datetime)):
        y, m, d = value.year, value.month, value.day
    else:
        y, m, d = value
    pad = cfg.get("pad", True)
    parts = {
        "year": "%04d" % y if pad else str(y),
        "month": "%02d" % m if pad else str(m),
        "day": "%02d" % d if pad else str(d),
    }
    return cfg["separator"].join(parts[k] for k in cfg["order"])


# ---------------------------------------------------------------- 配置校验

def validate_config(locales):
    for name, cfg in locales.items():
        for key in ("plural_rules", "number", "date", "fallback"):
            if key not in cfg:
                raise ConfigError("locale %r: missing key %r" % (name, key))
        rules = cfg["plural_rules"]
        if not rules:
            raise ConfigError("locale %r: plural_rules is empty" % name)
        cats = [r["category"] for r in rules]
        if "other" not in cats:
            raise ConfigError("locale %r: plural_rules must contain category 'other'" % name)
        if len(cats) != len(set(cats)):
            raise ConfigError("locale %r: duplicate plural categories" % name)
        has_catchall = False
        for r in rules:
            if "category" not in r or "when" not in r or not r["when"]:
                raise ConfigError("locale %r: every rule needs 'category' and non-empty 'when'" % name)
            for cond in r["when"]:
                op = cond.get("op")
                if op not in _KNOWN_OPS:
                    raise ConfigError("locale %r: unknown plural op %r" % (name, op))
                if op == "always":
                    has_catchall = True
                if op in ("mod_eq", "mod_in", "mod_not_in"):
                    if not isinstance(cond.get("mod"), int) or cond["mod"] <= 0:
                        raise ConfigError("locale %r: 'mod' must be a positive int" % name)
        if not has_catchall:
            raise ConfigError("locale %r: plural_rules need an 'always' catch-all rule" % name)
        num = cfg["number"]
        if not isinstance(num.get("group"), int) or num["group"] < 1:
            raise ConfigError("locale %r: number.group must be a positive int" % name)
        if not isinstance(num.get("max_decimals"), int) or num["max_decimals"] < 0:
            raise ConfigError("locale %r: number.max_decimals must be >= 0" % name)
        order = cfg["date"].get("order")
        if sorted(order or []) != ["day", "month", "year"]:
            raise ConfigError("locale %r: date.order must be a permutation of year/month/day" % name)
        for fb in cfg["fallback"]:
            if fb not in locales:
                raise ConfigError("locale %r: fallback target %r is not a known locale" % (name, fb))
            if fb == name:
                raise ConfigError("locale %r: cannot fall back to itself" % name)


# ---------------------------------------------------------------- 渲染结果

@dataclass
class RenderResult:
    text: str
    requested_locale: str
    locale_used: str           # 实际使用的语言（回退后与请求可能不同）
    chain: list                # 尝试过的回退链
    warnings: list = field(default_factory=list)   # 如占位符重复使用
    plurals: list = field(default_factory=list)    # PluralDecision，可解释


# ---------------------------------------------------------------- 格式化器

class Formatter:
    def __init__(self, locales, messages):
        validate_config(locales)
        self.locales = locales
        self.messages = messages
        self._compiled = {}

    @classmethod
    def from_files(cls, locales_path, messages_path):
        with open(locales_path, encoding="utf-8") as f:
            locales = json.load(f)
        with open(messages_path, encoding="utf-8") as f:
            messages = json.load(f)
        return cls(locales, messages)

    def chain(self, locale):
        """按声明顺序展开回退链（含自身，去重，防环）。"""
        if locale not in self.locales:
            raise ConfigError("unknown locale %r" % locale)
        chain = []
        stack = [locale]
        while stack:
            loc = stack.pop(0)
            if loc in chain:
                continue
            chain.append(loc)
            stack.extend(self.locales[loc]["fallback"])
        return chain

    def _compile(self, locale, key):
        cache_key = (locale, key)
        if cache_key not in self._compiled:
            self._compiled[cache_key] = parse_template(self.messages[locale][key])
        return self._compiled[cache_key]

    def validate_args(self, nodes, args):
        """返回 (missing, extra, duplicates)；每项都带名称与位置。"""
        uses = collect_placeholders(nodes)
        required = []
        for name, _ in uses:
            if name not in required:
                required.append(name)
        missing = [(n, p) for n, p in uses if n not in args]
        extra = sorted(set(args) - set(required))
        seen = {}
        duplicates = []
        for n, p in uses:
            seen.setdefault(n, []).append(p)
        for n, positions in seen.items():
            if len(positions) > 1:
                duplicates.append((n, positions))
        return missing, extra, duplicates

    def format(self, locale, key, **args):
        chain = self.chain(locale)
        used = None
        for loc in chain:
            if key in self.messages.get(loc, {}):
                used = loc
                break
        if used is None:
            raise MissingMessageError(
                "message %r not found in fallback chain %s" % (key, " -> ".join(chain)))
        nodes = self._compile(used, key)
        missing, extra, duplicates = self.validate_args(nodes, args)
        problems = []
        for n, p in missing:
            problems.append("missing argument %r (placeholder at position %d)" % (n, p))
        for n in extra:
            problems.append("extra argument %r (not used by template)" % n)
        if problems:
            raise MessageFormatError(
                "message %r [%s]: %s" % (key, used, "; ".join(problems)))
        warnings = []
        for n, positions in duplicates:
            warnings.append("placeholder %r used %d times at positions %s"
                            % (n, len(positions), positions))
        cfg = self.locales[used]
        result = RenderResult(text="", requested_locale=locale, locale_used=used,
                              chain=chain, warnings=warnings)
        ctx = {"args": args, "cfg": cfg, "result": result, "current_n": None}
        result.text = self._render(nodes, ctx)
        return result

    def _render(self, nodes, ctx):
        out = []
        for node in nodes:
            if isinstance(node, Text):
                out.append(node.value)
            elif isinstance(node, Placeholder):
                out.append(self._format_arg(ctx["args"][node.name], ctx["cfg"]))
            elif isinstance(node, NumberRef):
                if ctx["current_n"] is None:
                    raise MessageFormatError("'#' used outside of a plural block")
                out.append(format_number(ctx["current_n"], ctx["cfg"]["number"]))
            elif isinstance(node, Plural):
                n = ctx["args"][node.name]
                decision = decide_plural(n, ctx["cfg"]["plural_rules"])
                ctx["result"].plurals.append(decision)
                if decision.category not in node.options:
                    if "other" not in node.options:
                        raise MessageFormatError(
                            "plural for %r has no branch for category %r (and no 'other')"
                            % (node.name, decision.category))
                    branch = node.options["other"]
                else:
                    branch = node.options[decision.category]
                prev = ctx["current_n"]
                ctx["current_n"] = n
                out.append(self._render(branch, ctx))
                ctx["current_n"] = prev
        return "".join(out)

    def _format_arg(self, value, cfg):
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, (int, float)):
            return format_number(value, cfg["number"])
        if isinstance(value, (datetime.date, datetime.datetime)):
            return format_date(value, cfg["date"])
        if isinstance(value, tuple) and len(value) == 3:
            return format_date(value, cfg["date"])
        return str(value)
