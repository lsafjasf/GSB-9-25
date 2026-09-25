"""logtpl — 日志模板抽取与聚类库（仅标准库）。

核心思想：
1. 用一组有固定优先级的正则识别可变片段（时间、IP、UUID、时长、路径、
   URL、版本号、十六进制、数字），替换为分类占位符（<NUM>/<IP>/...）。
2. 模板 = 字面量片段 + 占位符片段的序列。给定原始变量值即可逐字符还原
   原始行（可变部分除外），见 Template.render / extract_values。
3. 聚类键 = 模板字符串本身，因此聚类结果与输入顺序完全无关；
   输出统一按 (-count, template) 排序，保证确定性。
4. 字段顺序调整（如 k=v 对换序）默认视为不同模板（模板是位置敏感的），
   可选 sort_kv=True 将相邻 k=v 片段按 key 排序后归并为同一模板。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Sequence, Tuple

# ---------------------------------------------------------------------------
# 可变片段识别。顺序即优先级：先匹配的模式优先，保证归类确定、与输入无关。
# ---------------------------------------------------------------------------
_VAR_SPECS: List[Tuple[str, str]] = [
    ("DATETIME", r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?"),
    ("URL",      r"https?://[^\s\"'\])}]+"),
    ("IP",       r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b"),
    ("UUID",     r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"),
    ("DURATION", r"\b\d+(?:\.\d+)?(?:ns|us|ms|min|s|h|d)\b"),
    ("PATH",     r"(?:[A-Za-z]:)?(?:/[A-Za-z0-9._~+\-]+){2,}/?"),
    ("VER",      r"\bv?\d+(?:\.\d+){1,3}\b"),
    ("HEX",      r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{16,}\b"),
    ("ID",       r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,}\b"),
    ("NUM",      r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])"),
]

_VAR_RE = re.compile("|".join("(?P<%s>%s)" % (name, pat) for name, pat in _VAR_SPECS))

# 结构化模式专用：k=v 整体识别，key 保留为字面量，value 归为 <VAL>。
# 优先级最低，且仅在 kv_vars=True 时启用，避免影响默认分类。
_KV_SPEC = ("KV", r"\b[A-Za-z_][\w.\-]*=[^\s=]+")
_VAR_RE_KV = re.compile(
    "|".join("(?P<%s>%s)" % (n, p) for n, p in _VAR_SPECS + [_KV_SPEC])
)

PLACEHOLDER_RE = re.compile(r"<([A-Z]+)>")

# k=v 词法：key 以字母/下划线开头，value 不含空白
_KV_TOKEN_RE = re.compile(r"^[A-Za-z_][\w.\-]*=\S+$")


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Template:
    """一条模板：字面量与占位符交替的片段序列。

    parts 元素为 ("lit", text) 或 ("var", NAME, value)。
    value 是首次抽取时该占位符对应的原文，仅用于示例/还原验证。
    """

    parts: Tuple[Tuple[str, ...], ...]
    text: str
    placeholders: Tuple[str, ...]

    def render(self, values: Sequence[str]) -> str:
        """用变量值序列还原原始行。values 顺序与 placeholders 一一对应。"""
        out: List[str] = []
        it = iter(values)
        for part in self.parts:
            if part[0] == "lit":
                out.append(part[1])
            else:
                out.append(next(it))
        return "".join(out)

    def extract_values(self, line: str) -> List[str]:
        """从一行中按模板字面量切出变量值；行不匹配模板时抛 ValueError。"""
        values: List[str] = []
        pos = 0
        for i, part in enumerate(self.parts):
            if part[0] == "lit":
                lit = part[1]
                if not line.startswith(lit, pos):
                    raise ValueError("line does not match template literal %r" % lit)
                pos += len(lit)
            else:
                # 变量延伸到下一个字面量（或行尾）
                nxt = ""
                for p2 in self.parts[i + 1:]:
                    if p2[0] == "lit" and p2[1]:
                        nxt = p2[1]
                        break
                if nxt:
                    idx = line.find(nxt, pos)
                    if idx < 0:
                        raise ValueError("line does not match template")
                    values.append(line[pos:idx])
                    pos = idx
                else:
                    values.append(line[pos:])
                    pos = len(line)
        if pos != len(line):
            raise ValueError("line has trailing content not in template")
        return values


def extract_template(line: str, *, kv_vars: bool = False) -> Template:
    """从单行日志抽取模板。

    kv_vars=True 时把 k=v 的 value 识别为 <VAL> 变量（key 保留为字面量），
    用于结构化日志的归并。
    """
    var_re = _VAR_RE_KV if kv_vars else _VAR_RE
    parts: List[Tuple[str, ...]] = []
    placeholders: List[str] = []
    text_parts: List[str] = []
    pos = 0
    for m in var_re.finditer(line):
        if m.start() > pos:
            lit = line[pos:m.start()]
            parts.append(("lit", lit))
            text_parts.append(lit)
        name = m.lastgroup
        value = m.group()
        if name == "KV":
            key, _, val = value.partition("=")
            parts.append(("lit", key + "="))
            text_parts.append(key + "=")
            name, value = "VAL", val
        parts.append(("var", name, value))
        placeholders.append(name)
        text_parts.append("<%s>" % name)
        pos = m.end()
    if pos < len(line):
        lit = line[pos:]
        parts.append(("lit", lit))
        text_parts.append(lit)
    return Template(tuple(parts), "".join(text_parts), tuple(placeholders))


# ---------------------------------------------------------------------------
# k=v 规范化（可选）：把相邻 k=v 词元按 key 排序，使字段换序的日志归并
# ---------------------------------------------------------------------------
def canonicalize_kv(line: str) -> str:
    tokens = line.split(" ")
    out: List[str] = []
    run: List[str] = []

    def flush() -> None:
        if len(run) >= 2:
            out.extend(sorted(run, key=lambda t: t.split("=", 1)[0].lower()))
        else:
            out.extend(run)
        del run[:]

    for tok in tokens:
        if _KV_TOKEN_RE.match(tok):
            run.append(tok)
        else:
            flush()
            out.append(tok)
    flush()
    return " ".join(out)


# ---------------------------------------------------------------------------
# 聚类
# ---------------------------------------------------------------------------
@dataclass
class Cluster:
    template: str
    placeholders: Tuple[str, ...]
    count: int
    example: str


@dataclass
class ClusterResult:
    total: int
    clusters: List[Cluster]
    # 未聚类行：(行内容, 出现次数)，按行内容排序保证确定性
    unclustered: List[Tuple[str, int]]

    @property
    def clustered_lines(self) -> int:
        return sum(c.count for c in self.clusters)

    @property
    def coverage(self) -> float:
        """模板覆盖率：被模板解释的行占比。"""
        return self.clustered_lines / self.total if self.total else 0.0

    @property
    def avg_placeholders(self) -> float:
        """平均占位符数（按行加权）。"""
        n = self.clustered_lines
        if not n:
            return 0.0
        return sum(len(c.placeholders) * c.count for c in self.clusters) / n

    def template_set(self) -> List[Tuple[str, int, Tuple[str, ...]]]:
        """(模板, 计数, 占位符归类) 的确定性列表，用于顺序无关性断言。"""
        return [(c.template, c.count, c.placeholders) for c in self.clusters]


def cluster_lines(
    lines: Iterable[str],
    *,
    min_literal_letters: int = 1,
    sort_kv: bool = False,
) -> ClusterResult:
    """对日志行聚类。

    min_literal_letters: 模板字面量部分至少包含的字母数，不足则该行
        视为"无稳定锚点"，进入 unclustered 而不计入覆盖率。
    sort_kv: 是否先做 k=v 字段排序规范化（合并字段换序的日志）。
    """
    letter_re = re.compile(r"[A-Za-z]")
    clusters: dict[str, Cluster] = {}
    unclustered: dict[str, int] = {}
    total = 0

    for raw in lines:
        line = raw.rstrip("\r\n")
        total += 1
        if sort_kv:
            line = canonicalize_kv(line)
        tpl = extract_template(line, kv_vars=sort_kv)
        literal_letters = sum(
            len(letter_re.findall(p[1])) for p in tpl.parts if p[0] == "lit"
        )
        if literal_letters < min_literal_letters:
            unclustered[line] = unclustered.get(line, 0) + 1
            continue
        c = clusters.get(tpl.text)
        if c is None:
            clusters[tpl.text] = Cluster(tpl.text, tpl.placeholders, 1, line)
        else:
            c.count += 1

    ordered = sorted(clusters.values(), key=lambda c: (-c.count, c.template))
    return ClusterResult(
        total=total,
        clusters=ordered,
        unclustered=sorted(unclustered.items()),
    )


def render_report(result: ClusterResult, max_unclustered: int = 20) -> str:
    """人类可读的聚类报告。"""
    out = [
        "total lines      : %d" % result.total,
        "templates        : %d" % len(result.clusters),
        "coverage         : %.2f%% (%d/%d)"
        % (result.coverage * 100, result.clustered_lines, result.total),
        "avg placeholders : %.2f" % result.avg_placeholders,
        "unclustered lines: %d" % sum(n for _, n in result.unclustered),
        "",
        "== templates (by count) ==",
    ]
    for c in result.clusters:
        out.append("[%6d] %s" % (c.count, c.template))
        out.append("         placeholders: %s" % (", ".join(c.placeholders) or "-"))
        out.append("         example     : %s" % c.example)
    if result.unclustered:
        out.append("")
        out.append("== unclustered lines ==")
        for line, n in result.unclustered[:max_unclustered]:
            out.append("[%6d] %s" % (n, line))
        if len(result.unclustered) > max_unclustered:
            out.append("... and %d more distinct lines"
                       % (len(result.unclustered) - max_unclustered))
    return "\n".join(out)
