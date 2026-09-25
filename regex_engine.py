"""regex_engine.py — 简易正则匹配引擎（库）

实现路线：解析 -> AST -> Thompson NFA -> Pike VM 模拟。
不使用任何回溯，因此不存在指数级重复搜索：
  时间复杂度 O(m * n)，空间复杂度 O(m)
  m = NFA 状态数（与模式长度线性相关），n = 输入长度。

支持语法（与 Python re 的子集语义对齐，re.search 语义）：
  字面字符、. （除 \\n 外任意字符）、[...] 与 [^...]（含区间、转义类）、
  量词 * + ?、转义（\\d \\D \\w \\W \\s \\S 及标点转义）、锚点 ^ $。
  $ 与 re 一致：匹配串尾或结尾换行符之前的位置。

语法错误抛出 RegexSyntaxError，携带位置与原因，绝不静默按字面量处理。
"""

MAXCHAR = 0x10FFFF

_DIGIT_RANGES = ((0x30, 0x39),)
_WORD_RANGES = ((0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A))
_SPACE_RANGES = ((0x09, 0x0D), (0x20, 0x20))


def _complement(ranges):
    out = []
    prev = 0
    for lo, hi in sorted(ranges):
        if lo > prev:
            out.append((prev, lo - 1))
        prev = max(prev, hi + 1)
    if prev <= MAXCHAR:
        out.append((prev, MAXCHAR))
    return tuple(out)


_CLASS_ESCAPES = {
    "d": _DIGIT_RANGES,
    "w": _WORD_RANGES,
    "s": _SPACE_RANGES,
    "D": _complement(_DIGIT_RANGES),
    "W": _complement(_WORD_RANGES),
    "S": _complement(_SPACE_RANGES),
}


class RegexSyntaxError(Exception):
    """语法错误：pos 为模式中的字符下标，reason 为原因。"""

    def __init__(self, pos, reason):
        self.pos = pos
        self.reason = reason
        super().__init__("position %d: %s" % (pos, reason))


# ---------------------------------------------------------------- AST 节点
class Lit:
    __slots__ = ("ch",)

    def __init__(self, ch):
        self.ch = ch


class Any:
    __slots__ = ()


class Class:
    __slots__ = ("ranges", "negated")

    def __init__(self, ranges, negated):
        self.ranges = ranges
        self.negated = negated


class Rep:
    __slots__ = ("node", "lo", "hi")

    def __init__(self, node, lo, hi):
        self.node = node
        self.lo = lo
        self.hi = hi  # None 表示无上界


class Seq:
    __slots__ = ("items",)

    def __init__(self, items):
        self.items = items


class Start:
    __slots__ = ()


class End:
    __slots__ = ()


# ---------------------------------------------------------------- 解析
class _Parser:
    def __init__(self, pattern):
        self.p = pattern
        self.i = 0

    def error(self, pos, reason):
        raise RegexSyntaxError(pos, reason)

    def parse(self):
        items = []
        while self.i < len(self.p):
            atom = self._atom()
            items.append(self._quantifier(atom))
        return Seq(items)

    def _atom(self):
        c = self.p[self.i]
        if c in "*+?":
            self.error(self.i, "量词缺少操作数 (nothing to repeat: %r)" % c)
        if c == "^":
            self.i += 1
            return Start()
        if c == "$":
            self.i += 1
            return End()
        if c == ".":
            self.i += 1
            return Any()
        if c == "[":
            return self._class()
        if c == "\\":
            return self._escape()
        self.i += 1
        return Lit(c)

    def _quantifier(self, atom):
        if self.i < len(self.p) and self.p[self.i] in "*+?":
            q = self.p[self.i]
            self.i += 1
            node = {
                "*": Rep(atom, 0, None),
                "+": Rep(atom, 1, None),
                "?": Rep(atom, 0, 1),
            }[q]
            if self.i < len(self.p) and self.p[self.i] in "*+?":
                self.error(
                    self.i,
                    "连续量词 (multiple repeat: %r 之后不允许再跟 %r)"
                    % (q, self.p[self.i]),
                )
            return node
        return atom

    def _escape(self):
        pos = self.i
        self.i += 1
        if self.i >= len(self.p):
            self.error(pos, "悬空转义 (bad escape: 模式以 \\ 结尾)")
        e = self.p[self.i]
        self.i += 1
        if e in _CLASS_ESCAPES:
            return Class(_CLASS_ESCAPES[e], False)
        if e.isascii() and e.isalnum():
            self.error(pos, "未知转义 (bad escape \\%s)" % e)
        return Lit(e)

    def _class(self):
        start = self.i
        self.i += 1  # 消费 '['
        negated = False
        if self.i < len(self.p) and self.p[self.i] == "^":
            negated = True
            self.i += 1
        ranges = []
        first = True
        while True:
            if self.i >= len(self.p):
                self.error(start, "未闭合字符类 (unterminated character set: 缺少 ])")
            if self.p[self.i] == "]" and not first:
                self.i += 1
                break
            first = False
            atom_ranges, is_char = self._class_atom(start)
            # 区间检测：后接 '-' 且 '-' 后还有非 ']' 字符
            if (
                self.i + 1 < len(self.p)
                and self.p[self.i] == "-"
                and self.p[self.i + 1] != "]"
            ):
                if not is_char:
                    self.error(
                        self.i,
                        "字符类区间起点非法 (bad character range: 起点不能是转义类)",
                    )
                self.i += 1  # 消费 '-'
                hi_ranges, hi_is_char = self._class_atom(start)
                if not hi_is_char:
                    self.error(
                        self.i - 1,
                        "字符类区间终点非法 (bad character range: 终点不能是转义类)",
                    )
                lo = atom_ranges[0][0]
                hi = hi_ranges[0][0]
                if hi < lo:
                    self.error(
                        self.i - 1,
                        "字符类区间反序 (bad character range: %s-%s)"
                        % (chr(lo), chr(hi)),
                    )
                ranges.append((lo, hi))
            else:
                ranges.extend(atom_ranges)
        return Class(_merge_ranges(ranges), negated)

    def _class_atom(self, class_start):
        c = self.p[self.i]
        if c == "\\":
            pos = self.i
            self.i += 1
            if self.i >= len(self.p):
                self.error(pos, "悬空转义 (bad escape: 字符类内以 \\ 结尾)")
            e = self.p[self.i]
            self.i += 1
            if e in _CLASS_ESCAPES:
                return list(_CLASS_ESCAPES[e]), False
            if e.isascii() and e.isalnum():
                self.error(pos, "未知转义 (bad escape \\%s)" % e)
            return [(ord(e), ord(e))], True
        self.i += 1
        return [(ord(c), ord(c))], True


def _merge_ranges(ranges):
    if not ranges:
        return ()
    out = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return tuple(out)


# ---------------------------------------------------------------- 编译为 NFA
# 指令: (CHAR, ch) (ANY,) (CLASS, idx) (SPLIT, x, y) (JMP, x)
#       (BOL,) (EOL,) (MATCH,)
CHAR, ANY, CLASS, SPLIT, JMP, BOL, EOL, MATCH = range(8)


class Prog:
    """编译后的程序。search(text) 返回 (start, end) 或 None。"""

    __slots__ = ("code", "classes", "pattern")

    def __init__(self, code, classes, pattern):
        self.code = code
        self.classes = classes
        self.pattern = pattern

    def search(self, text):
        return _pike_search(self, text)


def _compile(node, code, classes):
    if isinstance(node, Lit):
        code.append((CHAR, node.ch))
    elif isinstance(node, Any):
        code.append((ANY,))
    elif isinstance(node, Class):
        classes.append((node.ranges, node.negated))
        code.append((CLASS, len(classes) - 1))
    elif isinstance(node, Start):
        code.append((BOL,))
    elif isinstance(node, End):
        code.append((EOL,))
    elif isinstance(node, Seq):
        for item in node.items:
            _compile(item, code, classes)
    elif isinstance(node, Rep):
        if node.lo == 0 and node.hi is None:  # *
            l1 = len(code)
            code.append(None)
            _compile(node.node, code, classes)
            code.append((JMP, l1))
            code[l1] = (SPLIT, l1 + 1, len(code))
        elif node.lo == 1 and node.hi is None:  # +
            l1 = len(code)
            _compile(node.node, code, classes)
            code.append((SPLIT, l1, len(code) + 1))
        elif node.lo == 0 and node.hi == 1:  # ?
            l1 = len(code)
            code.append(None)
            _compile(node.node, code, classes)
            code[l1] = (SPLIT, l1 + 1, len(code))
        else:
            raise AssertionError("unsupported repetition")
    else:
        raise AssertionError("unknown node")


def compile(pattern):
    """编译模式，返回 Prog。语法错误抛 RegexSyntaxError。"""
    ast = _Parser(pattern).parse()
    code = []
    classes = []
    _compile(ast, code, classes)
    code.append((MATCH,))
    return Prog(code, classes, pattern)


# ---------------------------------------------------------------- Pike VM
def _class_contains(ranges, ch):
    o = ord(ch)
    lo, hi = 0, len(ranges) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        a, b = ranges[mid]
        if o < a:
            hi = mid - 1
        elif o > b:
            lo = mid + 1
        else:
            return True
    return False


def _add_thread(prog, text, i, lst, pc, start, visited):
    # 迭代式 epsilon 闭包，按优先级（先分支优先）展开；断言在当前位置 i 求值。
    stack = [pc]
    n = len(text)
    code = prog.code
    while stack:
        pc = stack.pop()
        if pc in visited:
            continue
        visited.add(pc)
        op = code[pc]
        kind = op[0]
        if kind == SPLIT:
            stack.append(op[2])
            stack.append(op[1])
        elif kind == JMP:
            stack.append(op[1])
        elif kind == BOL:
            if i == 0:
                stack.append(pc + 1)
        elif kind == EOL:
            if i == n or (i == n - 1 and text[i] == "\n"):
                stack.append(pc + 1)
        else:
            lst.append((pc, start))


def _pike_search(prog, text):
    code = prog.code
    classes = prog.classes
    n = len(text)
    clist = []
    clist_seen = set()
    matched = None
    for i in range(n + 1):
        if matched is None:
            # 每个位置都尝试开启新线程（优先级最低，附加在表尾）-> 左most 语义
            # 与 clist 共享去重集合：同一 pc 只保留最高优先级（起点最早）的线程，
            # 保证任意时刻线程数 <= 状态数 m，总耗时 O(m*n)。
            _add_thread(prog, text, i, clist, 0, i, clist_seen)
        nlist = []
        nlist_seen = set()
        for pc, start in clist:
            op = code[pc]
            kind = op[0]
            if kind == CHAR:
                if i < n and text[i] == op[1]:
                    _add_thread(prog, text, i + 1, nlist, pc + 1, start, nlist_seen)
            elif kind == ANY:
                if i < n and text[i] != "\n":
                    _add_thread(prog, text, i + 1, nlist, pc + 1, start, nlist_seen)
            elif kind == CLASS:
                if i < n:
                    ranges, negated = classes[op[1]]
                    if _class_contains(ranges, text[i]) != negated:
                        _add_thread(prog, text, i + 1, nlist, pc + 1, start, nlist_seen)
            elif kind == MATCH:
                matched = (start, i)
                break  # 丢弃低优先级线程
        clist, clist_seen = nlist, nlist_seen
    return matched


def search(pattern, text):
    """便捷接口：编译并搜索，返回 (start, end) 或 None。"""
    return compile(pattern).search(text)
