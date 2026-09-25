"""简易正则匹配引擎（仅标准库，自身不依赖 re 模块）。

支持语法：
  字面字符、'.'、字符类 '[...]' 与取反 '[^...]'、区间 'a-z'、
  量词 '*' '+' '?' 与 '{m}' '{m,}' '{m,n}' '{,n}'（含惰性后缀 '?'）、
  转义（\\d \\D \\w \\W \\s \\S \\n \\t \\r \\f \\v \\a \\0 以及元字符转义）、
  锚点 '^' '$' 与零宽断言 \\A \\Z \\b \\B，
  量词惰性后缀 '?'（对匹配结论无影响，仅解析兼容）。

语义对齐 Python re 的 search（非 DOTALL、非 MULTILINE）：
  '.' 不匹配 '\\n'；'^' 只匹配位置 0；'$' 匹配串尾或结尾唯一换行符之前。

已知差异（对拍时已排除）：
  不支持分组 '()' 与交替 '|'，它们在本引擎中按字面量处理；
  \\1-\\9 的组引用会报错（re 在无分组时同样报错）；
  占有量词后缀 '+'（如 'a*+'）会报错——它需要提交语义，NFA 模拟无法线性实现；
  重复次数上限为 MAX_REPEAT（防止病态内存占用），re 无此限制。

实现方式：Thompson NFA + 子集构造式模拟（逐字符推进活跃状态集合），
没有任何回溯，因此不存在指数级重复搜索。
设 m 为 NFA 状态数（m <= 2 * 编译后模式长度），n 为输入长度：
  时间复杂度 O(m * n)，空间复杂度 O(m)。
"""

MAX_REPEAT = 10000


class RegexSyntaxError(Exception):
    """模式语法错误，携带位置与原因。"""

    def __init__(self, pos, reason):
        self.pos = pos
        self.reason = reason
        super().__init__(f"位置 {pos}: {reason}")


# ---------------------------------------------------------------- 解析

_SIMPLE_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', 'f': '\f', 'v': '\v',
                   'a': '\x07', '0': '\x00'}


def _is_digit(ch):
    return '0' <= ch <= '9' or ch.isdecimal()


def _is_word(ch):
    return ch == '_' or ch.isalnum()


def _is_space(ch):
    return ch in ' \t\n\r\f\v' or ch.isspace()


def _read_hex(s, i, n, count, pos, esc):
    """从 s[i] 起读 count 位十六进制数字，不足则报错。"""
    digits = s[i:i + count]
    if len(digits) != count or any(c not in '0123456789abcdefABCDEF' for c in digits):
        raise RegexSyntaxError(pos, f"转义 '\\{esc}' 后需要 {count} 位十六进制数字")
    return chr(int(digits, 16)), i + count


def _read_octal(s, i, n):
    """s[i] 已是八进制数字，最多再读 2 位，返回 (字符, 新位置)。"""
    j = i
    while j < n and j < i + 3 and s[j] in '01234567':
        j += 1
    return chr(int(s[i:j], 8)), j


def _is_boundary(text, pos, n):
    prev_word = pos > 0 and _is_word(text[pos - 1])
    cur_word = pos < n and _is_word(text[pos])
    return prev_word != cur_word


_CLASS_PREDS = {
    'd': _is_digit,
    'D': lambda ch: not _is_digit(ch),
    'w': _is_word,
    'W': lambda ch: not _is_word(ch),
    's': _is_space,
    'S': lambda ch: not _is_space(ch),
}


class _Parser:
    def __init__(self, pattern):
        if not isinstance(pattern, str):
            raise TypeError("模式必须是 str")
        self.s = pattern
        self.n = len(pattern)
        self.i = 0

    def parse(self):
        nodes = []
        while self.i < self.n:
            nodes.append(self._term())
        return ('seq', nodes)

    # term := atom quantifier?
    def _term(self):
        c = self.s[self.i]
        if c in '*+?':
            raise RegexSyntaxError(self.i, f"量词 '{c}' 缺少操作数")
        if c == '{' and self._brace_at(self.i) is not None:
            raise RegexSyntaxError(self.i, "量词 '{...}' 缺少操作数")
        atom = self._atom()
        qpos = self.i
        bounds = self._peek_quantifier()
        if bounds is None:
            return atom
        if atom[0] == 'anchor':
            raise RegexSyntaxError(qpos, "量词缺少操作数（锚点 '^'/'$' 是零宽断言，不能被量化）")
        if self.i < self.n and self.s[self.i] == '?':
            self.i += 1  # 惰性后缀只影响优先级，不影响“是否匹配”的结论
        elif self.i < self.n and self.s[self.i] == '+':
            # 占有量词需要“提交”语义，NFA 模拟无法在线性时间内实现，明确拒绝
            raise RegexSyntaxError(self.i, "占有量词后缀 '+' 不受支持")
        mq = self.i
        if self._peek_quantifier() is not None:
            raise RegexSyntaxError(mq, f"多重量词 '{self.s[mq]}'")
        lo, hi = bounds
        if (lo, hi) in ((0, 1), (0, None), (1, None)):
            return ('rep', atom, lo, hi)
        # {m,n} 展开为 m 份必选 + (n-m) 份可选 / 一份星号
        parts = [atom] * lo
        if hi is None:
            parts.append(('rep', atom, 0, None))
        else:
            parts.extend([('rep', atom, 0, 1)] * (hi - lo))
        return ('seq', parts)

    def _peek_quantifier(self):
        """若当前位置是量词则消费并返回 (lo, hi)，否则返回 None。hi=None 表示无上界。"""
        if self.i >= self.n:
            return None
        c = self.s[self.i]
        if c == '*':
            self.i += 1
            return (0, None)
        if c == '+':
            self.i += 1
            return (1, None)
        if c == '?':
            self.i += 1
            return (0, 1)
        if c == '{':
            parsed = self._brace_at(self.i)
            if parsed is None:
                return None
            lo, hi, end = parsed
            self.i = end
            return (lo, hi)
        return None

    def _brace_at(self, i):
        """解析 s[i] 起的 '{m}' '{m,}' '{m,n}' '{,n}'，非法则返回 None（按字面量处理）。"""
        n = self.n
        j = i + 1
        k = j
        while k < n and self.s[k].isdigit():
            k += 1
        lo = int(self.s[j:k]) if k > j else 0
        if k < n and self.s[k] == '}':
            if k == j:
                return None  # '{}' 按字面量
            self._check_repeat(lo, i)
            return (lo, lo, k + 1)
        if k < n and self.s[k] == ',':
            k += 1
            m = k
            while k < n and self.s[k].isdigit():
                k += 1
            hi = int(self.s[m:k]) if k > m else None
            if k < n and self.s[k] == '}':
                if hi is not None:
                    if hi < lo:
                        raise RegexSyntaxError(i, f"重复区间反序 '{{{lo},{hi}}}'（下界大于上界）")
                    self._check_repeat(hi, i)
                self._check_repeat(lo, i)
                return (lo, hi, k + 1)
        return None

    @staticmethod
    def _check_repeat(count, pos):
        if count > MAX_REPEAT:
            raise RegexSyntaxError(pos, f"重复次数 {count} 超过上限 {MAX_REPEAT}")

    # atom := '.' | '^' | '$' | '[' class ']' | '\' escape | literal
    def _atom(self):
        c = self.s[self.i]
        self.i += 1
        if c == '.':
            return ('dot',)
        if c in '^$':
            return ('anchor', c)
        if c == '[':
            return self._class(self.i - 1)
        if c == '\\':
            return self._escape(self.i - 1)
        return ('lit', c)

    def _escape(self, pos):
        if self.i >= self.n:
            raise RegexSyntaxError(pos, "悬空转义：'\\' 出现在模式末尾，后面缺少字符")
        c = self.s[self.i]
        self.i += 1
        if c in _CLASS_PREDS:
            return ('class', False, (('pred', c),))
        if c in 'AZbB':
            return ('anchor', c)
        if c in _SIMPLE_ESCAPES:
            return ('lit', _SIMPLE_ESCAPES[c])
        if c == '0':
            ch, self.i = _read_octal(self.s, self.i - 1, self.n)
            if ord(ch) > 0o377:
                raise RegexSyntaxError(pos, "八进制转义超出范围 0-0o377")
            return ('lit', ch)
        if c in 'xuU':
            ch, self.i = _read_hex(self.s, self.i, self.n,
                                   {'x': 2, 'u': 4, 'U': 8}[c], pos, c)
            return ('lit', ch)
        if c.isdigit():
            raise RegexSyntaxError(pos, f"无效的组引用 '\\{c}'（本引擎不支持分组）")
        if c.isalnum():
            raise RegexSyntaxError(pos, f"未知转义 '\\{c}'")
        return ('lit', c)

    def _class(self, start):
        negated = False
        if self.i < self.n and self.s[self.i] == '^':
            negated = True
            self.i += 1
        items = []
        first = True
        while True:
            if self.i >= self.n:
                raise RegexSyntaxError(start, "未闭合的字符类 '['")
            if self.s[self.i] == ']' and not first:
                self.i += 1
                break
            first = False
            lo_pos = self.i
            lo = self._class_item()
            if (self.i < self.n and self.s[self.i] == '-'
                    and self.i + 1 < self.n and self.s[self.i + 1] != ']'):
                if lo[0] != 'char':
                    raise RegexSyntaxError(lo_pos, "字符类区间端点不能是预定义字符类")
                self.i += 1
                hi = self._class_item()
                if hi[0] != 'char':
                    raise RegexSyntaxError(lo_pos, "字符类区间端点不能是预定义字符类")
                if hi[1] < lo[1]:
                    raise RegexSyntaxError(lo_pos,
                                           f"字符类区间反序 '{lo[1]}-{hi[1]}'（起点大于终点）")
                items.append(('range', lo[1], hi[1]))
            else:
                items.append(lo)
        return ('class', negated, tuple(items))

    def _class_item(self):
        c = self.s[self.i]
        if c != '\\':
            self.i += 1
            return ('char', c)
        pos = self.i
        self.i += 1
        if self.i >= self.n:
            raise RegexSyntaxError(pos, "悬空转义：'\\' 后面缺少字符")
        e = self.s[self.i]
        self.i += 1
        if e in _CLASS_PREDS:
            return ('pred', e)
        if e == 'b':
            return ('char', '\x08')  # 字符类内 \b 是退格符（与 re 一致）
        if e in _SIMPLE_ESCAPES:
            return ('char', _SIMPLE_ESCAPES[e])
        if e in '01234567':
            ch, self.i = _read_octal(self.s, self.i - 1, self.n)
            if ord(ch) > 0o377:
                raise RegexSyntaxError(pos, "八进制转义超出范围 0-0o377")
            return ('char', ch)
        if e in 'xuU':
            ch, self.i = _read_hex(self.s, self.i, self.n,
                                   {'x': 2, 'u': 4, 'U': 8}[e], pos, e)
            return ('char', ch)
        if e.isalnum():
            raise RegexSyntaxError(pos, f"未知转义 '\\{e}'")
        return ('char', e)


# ---------------------------------------------------------------- 编译为 NFA

_CHAR = 0
_DOT = 1
_CLASS = 2
_SPLIT = 3
_JMP = 4
_ANCHOR = 5
_ACCEPT = 6


def _build_class_matcher(negated, items):
    chars = set()
    ranges = []
    preds = []
    for item in items:
        if item[0] == 'char':
            chars.add(item[1])
        elif item[0] == 'range':
            ranges.append((item[1], item[2]))
        else:
            preds.append(_CLASS_PREDS[item[1]])
    return (negated, frozenset(chars), tuple(ranges), tuple(preds))


def _emit(node, insts):
    """把 AST 编译进 insts（指令为可变列表），返回 (start_pc, outs)。

    outs 是 [(pc, slot)]，表示 insts[pc][slot] 待回填为下一条指令。
    """
    kind = node[0]
    if kind == 'seq':
        start = None
        outs = []
        for child in node[1]:
            cstart, couts = _emit(child, insts)
            if start is None:
                start = cstart
            else:
                for pc, slot in outs:
                    insts[pc][slot] = cstart
            outs = couts
        if start is None:  # 空序列：一条跳转占位
            pc = len(insts)
            insts.append([_JMP, 0, 0])
            return pc, [(pc, 1)]
        return start, outs
    if kind == 'lit':
        pc = len(insts)
        insts.append([_CHAR, node[1], 0])
        return pc, [(pc, 2)]
    if kind == 'dot':
        pc = len(insts)
        insts.append([_DOT, 0, 0])
        return pc, [(pc, 2)]
    if kind == 'class':
        pc = len(insts)
        insts.append([_CLASS, _build_class_matcher(node[1], node[2]), 0])
        return pc, [(pc, 2)]
    if kind == 'anchor':
        pc = len(insts)
        insts.append([_ANCHOR, node[1], 0])
        return pc, [(pc, 2)]
    if kind == 'rep':
        atom, lo, hi = node[1], node[2], node[3]
        if hi == 1:  # ?
            pc = len(insts)
            insts.append([_SPLIT, 0, 0])
            bstart, bouts = _emit(atom, insts)
            insts[pc][1] = bstart
            return pc, bouts + [(pc, 2)]
        if lo == 0:  # *
            pc = len(insts)
            insts.append([_SPLIT, 0, 0])
            bstart, bouts = _emit(atom, insts)
            insts[pc][1] = bstart
            for p, s in bouts:
                insts[p][s] = pc  # 回边
            return pc, [(pc, 2)]
        # +
        bstart, bouts = _emit(atom, insts)
        pc = len(insts)
        insts.append([_SPLIT, bstart, 0])
        for p, s in bouts:
            insts[p][s] = pc
        return bstart, [(pc, 2)]
    raise AssertionError(f"未知节点 {kind}")


def _class_match(matcher, ch):
    negated, chars, ranges, preds = matcher
    ok = ch in chars
    if not ok:
        for lo, hi in ranges:
            if lo <= ch <= hi:
                ok = True
                break
    if not ok:
        for pred in preds:
            if pred(ch):
                ok = True
                break
    return ok != negated


# ---------------------------------------------------------------- 模拟执行

def _add_state(insts, pc, clist, visited, pos, text, n):
    """沿 epsilon 边（jmp/split/anchor）闭包展开，把消费型状态加入 clist。"""
    stack = [pc]
    while stack:
        pc = stack.pop()
        if pc in visited:
            continue
        visited.add(pc)
        inst = insts[pc]
        op = inst[0]
        if op == _JMP:
            stack.append(inst[1])
        elif op == _SPLIT:
            stack.append(inst[2])
            stack.append(inst[1])
        elif op == _ANCHOR:
            kind = inst[1]
            if kind == '^' or kind == 'A':
                ok = pos == 0
            elif kind == '$':  # 串尾，或结尾唯一换行符之前（与 Python re 一致）
                ok = pos == n or (pos == n - 1 and text[pos] == '\n')
            elif kind == 'Z':
                ok = pos == n
            elif kind == 'b':
                ok = _is_boundary(text, pos, n)
            else:  # 'B'：Python <= 3.13 在空串上不匹配
                ok = n > 0 and not _is_boundary(text, pos, n)
            if ok:
                stack.append(inst[2])
        else:
            clist.append(pc)


class Pattern:
    """编译后的模式。search(text) 返回是否匹配（语义同 re.search(...) is not None）。"""

    __slots__ = ('pattern', '_insts', '_start')

    def __init__(self, pattern, insts, start):
        self.pattern = pattern
        self._insts = insts
        self._start = start

    def search(self, text):
        insts = self._insts
        n = len(text)
        clist = []
        visited = set()
        for pos in range(n + 1):
            # 注入一次从 pos 开始的新尝试，实现 search（非锚定）语义
            _add_state(insts, self._start, clist, visited, pos, text, n)
            for pc in clist:
                if insts[pc][0] == _ACCEPT:
                    return True
            if pos == n:
                return False
            ch = text[pos]
            nlist = []
            nvisited = set()
            npos = pos + 1
            for pc in clist:
                inst = insts[pc]
                op = inst[0]
                if op == _CHAR:
                    if inst[1] == ch:
                        _add_state(insts, inst[2], nlist, nvisited, npos, text, n)
                elif op == _DOT:
                    if ch != '\n':
                        _add_state(insts, inst[2], nlist, nvisited, npos, text, n)
                elif op == _CLASS:
                    if _class_match(inst[1], ch):
                        _add_state(insts, inst[2], nlist, nvisited, npos, text, n)
            clist = nlist
            visited = nvisited
        return False


def compile(pattern):
    """编译模式，返回 Pattern；语法错误抛出 RegexSyntaxError（含位置与原因）。"""
    ast = _Parser(pattern).parse()
    insts = []
    start, outs = _emit(ast, insts)
    accept = len(insts)
    insts.append([_ACCEPT])
    for pc, slot in outs:
        insts[pc][slot] = accept
    return Pattern(pattern, insts, start)


def search(pattern, text):
    """便捷函数：编译并搜索，返回是否匹配。"""
    return compile(pattern).search(text)


if __name__ == '__main__':
    import sys

    if len(sys.argv) != 3:
        print('用法: python3 regex_engine.py <模式> <文本>')
        sys.exit(2)
    try:
        prog = compile(sys.argv[1])
    except RegexSyntaxError as exc:
        print(f'语法错误: {exc}')
        sys.exit(1)
    print('匹配' if prog.search(sys.argv[2]) else '不匹配')
