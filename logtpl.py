"""logtpl — 日志模板抽取与聚类库（仅依赖 Python 标准库）。

核心思想：
  1. 用一个主正则把每行日志切分为 token（引号串、key=value、IP、时长、
     UUID、路径、数字、单词、空白、其它字符），主正则覆盖所有字符，无遗漏。
  2. 可变 token 替换为分类占位符（<STR> <KV值类型> <IP> <DUR> <UUID>
     <PATH> <NUM> <ID>），不可变部分逐字符保留为字面量。
  3. 模板 = (字面量/占位符) 序列。聚类键即模板本身，与输入顺序无关。
  4. 模板 + 按顺序捕获的变量值可逐字符还原原始行（round-trip 保证）。

字段顺序调整的日志（如 "a=1 b=2" 与 "b=2 a=1"）：
  模板是位置敏感的，二者会形成不同模板；但库会计算 canonical_key
  （kv 对排序后的签名），把"字段集合相同、顺序不同"的模板归入
  reorder_groups 显式报告，由调用方决定是否合并。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 分词

_TOKEN_RE = re.compile(r"""
      (?P<qstring>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
    | (?P<kv>[A-Za-z_][\w.-]*=(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s]+))
    | (?P<ip>\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?)
    | (?P<uuid>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})
    | (?P<duration>\d+(?:\.\d+)?(?:ns|us|ms|s|m|h)\b)
    | (?P<path>(?:\.{1,2}/|/)[^\s"',;()\[\]{}]*|(?:[\w.-]+/)+[\w.-]*)
    | (?P<id>(?=[A-Za-z0-9.-]*[A-Za-z])(?=[A-Za-z0-9.-]*\d)[A-Za-z0-9][A-Za-z0-9.-]{5,}
       | (?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,})
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<word>[A-Za-z_][\w.-]*)
    | (?P<ws>\s+)
    | (?P<other>[\s\S])
    """, re.VERBOSE)

_RE_QSTRING = re.compile(r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')\Z')
_RE_IP = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\Z')
_RE_UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z')
_RE_DURATION = re.compile(r'\d+(?:\.\d+)?(?:ns|us|ms|s|m|h)\Z')
_RE_PATH = re.compile(r'(?:(?:\.{1,2}/|/)\S*|(?:[\w.-]+/)+[\w.-]*)\Z')
_RE_NUMBER = re.compile(r'\d+(?:\.\d+)?\Z')
_RE_HEXID = re.compile(r'(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\Z')

_PLACEHOLDER = {
    'qstring': 'STR',
    'ip': 'IP',
    'uuid': 'UUID',
    'duration': 'DUR',
    'path': 'PATH',
    'id': 'ID',
    'number': 'NUM',
}

_ID_MIN_LEN = 6


def _is_id(text: str) -> bool:
    """字母+数字混合的长 token 视为标识符，如 8f3a2bc1、req00x9、deadbeef。"""
    if len(text) >= 8 and _RE_HEXID.match(text):
        return True
    return (len(text) >= _ID_MIN_LEN
            and any(c.isdigit() for c in text)
            and any(c.isalpha() for c in text))


def classify_value(val: str) -> str:
    """对 key=value 中的 value（或任意裸值）分类，返回占位符名。"""
    if _RE_QSTRING.match(val):
        return 'STR'
    if _RE_NUMBER.match(val):
        return 'NUM'
    if _RE_IP.match(val):
        return 'IP'
    if _RE_UUID.match(val):
        return 'UUID'
    if _RE_DURATION.match(val):
        return 'DUR'
    if _RE_PATH.match(val):
        return 'PATH'
    if _RE_HEXID.match(val):
        return 'ID'
    if _is_id(val):
        return 'ID'
    return 'STR'


def parse_line(line: str):
    """把一行日志解析为 (parts, values)。

    parts: tuple of ('lit', 字面文本) | ('var', 占位符名)
    values: list[str]，与 parts 中 'var' 项一一对应（原始文本，未做修改）
    保证 reconstruct(parts, values) == line。
    """
    parts: list[tuple[str, str]] = []
    values: list[str] = []
    for m in _TOKEN_RE.finditer(line):
        kind = m.lastgroup
        text = m.group()
        if kind == 'kv':
            key, _, val = text.partition('=')
            parts.append(('lit', key + '='))
            parts.append(('var', classify_value(val)))
            values.append(val)
        elif kind in _PLACEHOLDER:
            parts.append(('var', _PLACEHOLDER[kind]))
            values.append(text)
        elif kind == 'word':
            if _is_id(text):
                parts.append(('var', 'ID'))
                values.append(text)
            else:
                parts.append(('lit', text))
        else:  # ws / other —— 原样保留
            parts.append(('lit', text))
    # 合并相邻字面量，使模板更紧凑
    merged: list[tuple[str, str]] = []
    for part in parts:
        if merged and part[0] == 'lit' and merged[-1][0] == 'lit':
            merged[-1] = ('lit', merged[-1][1] + part[1])
        else:
            merged.append(part)
    return tuple(merged), values


def reconstruct(parts, values) -> str:
    """用模板 parts 和捕获的 values 还原原始行。"""
    out = []
    it = iter(values)
    for kind, x in parts:
        out.append(x if kind == 'lit' else next(it))
    return ''.join(out)


def render(parts) -> str:
    """把模板渲染为可读字符串，占位符写作 <NAME>。"""
    return ''.join(x if kind == 'lit' else '<%s>' % x for kind, x in parts)


def count_placeholders(parts) -> int:
    return sum(1 for kind, _ in parts if kind == 'var')


def canonical_key(parts):
    """字段重排检测签名：kv 对排序 + 其余字面/占位结构。

    两行仅 key=value 顺序不同、其余完全相同时，canonical_key 相同。
    """
    kvs = []
    lits = []
    i = 0
    n = len(parts)
    while i < n:
        kind, x = parts[i]
        if (kind == 'lit' and x.endswith('=') and i + 1 < n
                and parts[i + 1][0] == 'var'):
            kvs.append((x.strip(), parts[i + 1][1]))
            i += 2
        elif kind == 'lit':
            if x.strip():
                lits.append(('lit', x.strip()))
            i += 1
        else:
            lits.append(('var', x))
            i += 1
    return (tuple(sorted(kvs)), tuple(lits))


# ---------------------------------------------------------------- 聚类

@dataclass
class TemplateInfo:
    parts: tuple
    template: str
    count: int
    placeholders: int
    example: str


@dataclass
class Report:
    total_lines: int
    min_support: int
    templates: list            # 有效模板（count >= min_support），已排序
    num_templates_total: int   # 含无效（支持度不足）模板
    clustered_lines: int
    coverage: float
    avg_placeholders: float
    unclustered: list          # 未被任何有效模板解释的行（排序后）
    reorder_groups: list       # list[list[str]] 字段重排变体组


class Clusterer:
    """流式聚类器：内存占用 O(模板数)，与行数无关。

    min_support: 模板至少解释多少行才算"有效模板"（默认 2）。
    支持度不足的模板所覆盖的行计入 unclustered。
    """

    def __init__(self, min_support: int = 2):
        if min_support < 1:
            raise ValueError('min_support must be >= 1')
        self.min_support = min_support
        self.total = 0
        # parts -> [count, example_line, pending_lines|None]
        # pending_lines: count 达到 min_support 前暂存原始行，之后丢弃（省内存）
        self._clusters: dict[tuple, list] = {}

    def add(self, line: str) -> None:
        parts, _ = parse_line(line)
        self.total += 1
        entry = self._clusters.get(parts)
        if entry is None:
            pending = [line] if self.min_support > 1 else None
            self._clusters[parts] = [1, line, pending]
        else:
            entry[0] += 1
            if entry[2] is not None:
                entry[2].append(line)
                if entry[0] >= self.min_support:
                    entry[2] = None

    def add_all(self, lines) -> None:
        for line in lines:
            self.add(line)

    def report(self) -> Report:
        valid, unclustered = [], []
        for parts, (count, example, pending) in self._clusters.items():
            if count >= self.min_support:
                valid.append(TemplateInfo(
                    parts=parts,
                    template=render(parts),
                    count=count,
                    placeholders=count_placeholders(parts),
                    example=example,
                ))
            elif pending:
                unclustered.extend(pending)
        # 排序键只含模板内容与计数 → 输出与输入顺序无关
        valid.sort(key=lambda t: (-t.count, t.template))
        unclustered.sort()

        groups: dict = {}
        for t in valid:
            groups.setdefault(canonical_key(t.parts), []).append(t.template)
        reorder_groups = [sorted(ts) for ts in groups.values() if len(ts) > 1]
        reorder_groups.sort()

        clustered = sum(t.count for t in valid)
        avg_ph = (sum(t.placeholders for t in valid) / len(valid)) if valid else 0.0
        return Report(
            total_lines=self.total,
            min_support=self.min_support,
            templates=valid,
            num_templates_total=len(self._clusters),
            clustered_lines=clustered,
            coverage=(clustered / self.total) if self.total else 0.0,
            avg_placeholders=avg_ph,
            unclustered=unclustered,
            reorder_groups=reorder_groups,
        )


def cluster(lines, min_support: int = 2) -> Report:
    c = Clusterer(min_support=min_support)
    c.add_all(lines)
    return c.report()


# ---------------------------------------------------------------- 报告

def format_report(rep: Report, max_unclustered: int = 50) -> str:
    out = []
    a = out.append
    a('== logtpl report ==')
    a('total lines      : %d' % rep.total_lines)
    a('templates        : %d valid (support>=%d), %d total'
      % (len(rep.templates), rep.min_support, rep.num_templates_total))
    a('coverage         : %.2f%% (%d/%d lines explained by valid templates)'
      % (rep.coverage * 100, rep.clustered_lines, rep.total_lines))
    a('avg placeholders : %.2f per valid template' % rep.avg_placeholders)
    a('')
    a('-- templates (sorted by count desc, then template) --')
    for t in rep.templates:
        a('  [count=%-8d ph=%d] %s' % (t.count, t.placeholders, t.template))
    if rep.reorder_groups:
        a('')
        a('-- reorder-variant groups (same fields, different order; '
          'templates are position-sensitive so they stay separate) --')
        for i, g in enumerate(rep.reorder_groups, 1):
            a('  group %d:' % i)
            for t in g:
                a('    %s' % t)
    a('')
    a('-- unclustered lines (%d) --' % len(rep.unclustered))
    for line in rep.unclustered[:max_unclustered]:
        a('  %s' % line)
    if len(rep.unclustered) > max_unclustered:
        a('  ... and %d more' % (len(rep.unclustered) - max_unclustered))
    return '\n'.join(out)


def main(argv=None) -> int:
    import argparse
    import sys
    p = argparse.ArgumentParser(description='log template extraction & clustering')
    p.add_argument('file', nargs='?', help='log file (default: stdin)')
    p.add_argument('--min-support', type=int, default=2)
    args = p.parse_args(argv)

    if args.file:
        f = open(args.file, 'r', encoding='utf-8', errors='replace')
    else:
        f = sys.stdin
    c = Clusterer(min_support=args.min_support)
    with f:
        for line in f:
            c.add(line.rstrip('\n'))
    print(format_report(c.report()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
