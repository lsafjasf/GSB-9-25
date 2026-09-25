"""naive_parser.py —— 修复前的缺陷实现（仅用于复现缺陷，勿用于生产）。

已知缺陷：
1. 推断与切分都不感知引号，字段内含分隔符会被错误切开。
2. 按物理行切分，引号内换行被当成记录结束。
3. 转义引号（双写引号）不做还原。
4. 只有一行数据时走 raw-count 兜底路径，与多行的 quote-aware 投票路径结论不一致。
"""

DELIMITERS = [",", "\t", ";", "|"]
QUOTE = '"'


def _count_outside_quotes(line, delim):
    count = 0
    in_quote = False
    for ch in line:
        if ch == QUOTE:
            in_quote = not in_quote
        elif ch == delim and not in_quote:
            count += 1
    return count


def infer(text):
    """返回 (delimiter, quotechar)。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2:
        # 多行路径：quote-aware 投票
        best = None
        for d in DELIMITERS:
            counts = [_count_outside_quotes(ln, d) for ln in lines]
            if min(counts) > 0 and len(set(counts)) == 1:
                if best is None or counts[0] > best[1]:
                    best = (d, counts[0])
        if best is not None:
            return best[0], QUOTE
    # 单行兜底路径：直接对第一行做 raw count（不感知引号）—— 缺陷 1/4 来源
    first = lines[0] if lines else ""
    counts = {d: first.count(d) for d in DELIMITERS}
    return max(counts, key=lambda d: counts[d]), QUOTE


def parse(text):
    delim, quote = infer(text)
    rows = []
    for line in text.split("\n"):  # 缺陷 2：引号内换行被当成记录结束
        if line == "":
            continue
        fields = []
        for f in line.split(delim):  # 缺陷 1：字段内分隔符被切开
            if len(f) >= 2 and f.startswith(quote) and f.endswith(quote):
                f = f[1:-1]  # 缺陷 3：双写转义引号不还原
            fields.append(f)
        rows.append(fields)
    return rows
