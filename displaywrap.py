"""displaywrap — 按显示宽度折行与截断（Python 3，仅标准库）。

规则摘要
--------
* 显示宽度：东亚宽/全角字符计 2 列，其余可打印字符计 1 列；
  组合字符（Mn/Me）、零宽字符（Cf：ZWJ/ZWSP/ZWNJ/BOM 等）、控制字符计 0 列。
* 簇（cluster，不可拆散单元）：基字符 + 后续组合标记 / 变体选择符 /
  肤色修饰符；ZWJ 序列与旗帜（两个区域指示符）整体为一簇。
  折行与截断绝不拆散簇。Python 3 中代理对是单个码位，天然不会被拆开；
  孤立代理项按宽 1 的独立簇处理。
* 折行：优先在空格处断行；超宽单词按簇强制断开；调用方可通过 atoms
  声明禁拆片段，禁拆片段整体移动，若自身比行宽还宽则独占一行（允许溢出）。
* 空白：行首空白丢弃；断行点处的空白丢弃；行尾空白丢弃。
* 制表符：tab="expand" 按列位展开（每逻辑行重置列位，列位按显示宽度计），
  tab="reject" 遇到制表符抛 ValueError。展开与折行使用同一套宽度计算。
* 截断：truncate() 追加省略标记，省略标记计入宽度；按单行处理（首个
  换行后的内容忽略）。降级规则：可用宽度小于省略标记自身宽度时返回空串 ""。
* 流式：Wrapper.feed()/finish() 与一次性 wrap() 结果完全一致；
  内部缓冲为 O(width + 最长禁拆片段 + 最长簇)，与输入总量无关。
"""

from __future__ import annotations

import unicodedata

__all__ = [
    "char_width",
    "clusters",
    "cluster_width",
    "display_width",
    "expand_tabs",
    "wrap",
    "truncate",
    "Wrapper",
]

_ZWJ = "‍"
_VS16 = "️"
_KEYCAP = "⃣"
_HARD_BREAK_CHARS = "\t\n\r"  # 簇切分时的硬边界（各自独立成簇）


def _is_regional(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_emoji_modifier(ch: str) -> bool:
    return 0x1F3FB <= ord(ch) <= 0x1F3FF


def _is_extend(ch: str) -> bool:
    """是否应并入前一个簇（组合标记 / 变体选择符 / 肤色修饰符）。"""
    if _is_emoji_modifier(ch):
        return True
    return unicodedata.category(ch) in ("Mn", "Me")


def char_width(ch: str) -> int:
    """单个码位的显示宽度（0 / 1 / 2）。制表符计 0，需先用 expand_tabs 展开。"""
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Cf", "Cc"):
        return 0
    if _is_regional(ch):
        return 1  # 旗帜由两个区域指示符组成，整体宽 2（见 cluster_width）
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _next_cluster(s: str, i: int):
    """返回 (簇字符串, 下一位置)。"""
    n = len(s)
    ch = s[i]
    if ch in _HARD_BREAK_CHARS:
        return ch, i + 1
    j = i + 1
    if _is_regional(ch) and j < n and _is_regional(s[j]):
        j += 1  # 旗帜：两个区域指示符合为一簇
    while j < n:
        c = s[j]
        if c in _HARD_BREAK_CHARS:
            break
        if _is_extend(c):
            j += 1
        elif c == _ZWJ:
            j += 1
            if j < n and s[j] not in _HARD_BREAK_CHARS:
                j += 1  # ZWJ 与其后的字符一并并入
        else:
            break
    return s[i:j], j


def clusters(text: str) -> list[str]:
    """把文本切成不可拆散的簇序列。"""
    out = []
    i, n = 0, len(text)
    while i < n:
        cl, i = _next_cluster(text, i)
        out.append(cl)
    return out


def cluster_width(cluster: str) -> int:
    """簇的显示宽度。"""
    if not cluster:
        return 0
    if any(_is_regional(c) for c in cluster):
        return 2  # 旗帜
    if _VS16 in cluster or _KEYCAP in cluster:
        return 2  # emoji 表现形式的簇（如 ❤️、1️⃣）
    return max(char_width(c) for c in cluster)


def display_width(text: str) -> int:
    """文本的显示宽度。注意：制表符计 0（不展开），换行计 0。"""
    return sum(cluster_width(cl) for cl in clusters(text))


def expand_tabs(text: str, tabsize: int = 8) -> str:
    """按显示列位展开制表符；列位在每个逻辑行（\\n 分隔）内重置。"""
    if tabsize < 1:
        raise ValueError("tabsize 必须 >= 1")
    out = []
    col = 0
    for cl in clusters(text):
        if cl == "\t":
            n = tabsize - (col % tabsize)
            out.append(" " * n)
            col += n
        elif cl == "\n":
            out.append("\n")
            col = 0
        else:
            out.append(cl)
            col += cluster_width(cl)
    return "".join(out)


class _LineBuilder:
    """贪心行填充机：接收 词/空格/换行 事件，产出完整行。"""

    def __init__(self, width: int):
        if width < 1:
            raise ValueError("width 必须 >= 1")
        self.width = width
        self.parts: list[str] = []   # 当前行片段
        self.line_w = 0              # 当前行显示宽度
        self.pend: str | None = None  # 待定空格（后跟的词放不下则丢弃）
        self.pend_w = 0
        self.lines: list[str] = []   # 已完成行

    def _emit(self):
        self.lines.append("".join(self.parts))
        self.parts = []
        self.line_w = 0
        self.pend = None
        self.pend_w = 0

    def spaces(self, text: str, w: int):
        if not self.parts:
            return  # 行首空白丢弃
        if self.line_w + self.pend_w + w <= self.width:
            self.pend = (self.pend or "") + text
            self.pend_w += w
        else:
            self._emit()  # 空格放不下：断行并丢弃空格

    def word(self, units: list[tuple[str, int, bool]], final: bool = True):
        # units: (文本, 宽度, 是否禁拆)；final=False 表示同一词的后续片段还在路上
        if self.pend is not None:
            w = sum(u[1] for u in units)
            if self.line_w + self.pend_w + w <= self.width:
                self.parts.append(self.pend)
                self.line_w += self.pend_w
                self.pend = None
                self.pend_w = 0
            else:
                self._emit()  # 待定空格随断行丢弃
        self._consume(units)

    def _consume(self, units):
        for text, w, _atomic in units:
            if self.line_w + w <= self.width or not self.parts:
                # 行空时即使单元超宽（禁拆片段/单簇超宽）也整体放置，允许溢出
                self.parts.append(text)
                self.line_w += w
            else:
                self._emit()
                self.parts.append(text)
                self.line_w = w

    def newline(self):
        self._emit()  # 硬换行：产出当前行（可为空行），丢弃待定空格

    def finish(self):
        if self.parts:
            self._emit()


class Wrapper:
    """流式折行器。feed(chunk) 返回已完成行列表，finish() 返回剩余行。

    与一次性函数 wrap() 使用同一实现，结果保证一致。
    """

    def __init__(self, width: int, *, tab: str = "expand", tabsize: int = 8,
                 atoms=()):
        if width < 1:
            raise ValueError("width 必须 >= 1")
        if tab not in ("expand", "reject"):
            raise ValueError("tab 必须是 'expand' 或 'reject'")
        if tabsize < 1:
            raise ValueError("tabsize 必须 >= 1")
        atoms = list(atoms)
        if any(not a for a in atoms):
            raise ValueError("atoms 不能为空字符串")
        self._tab = tab
        self._tabsize = tabsize
        self._width = width
        # 最长优先，保证重叠禁拆片段按最长匹配
        self._atoms = sorted(set(atoms), key=len, reverse=True)
        self._max_atom_len = max((len(a) for a in self._atoms), default=0)
        self._b = _LineBuilder(width)
        self._buf = ""            # 未处理的原始文本尾部
        self._col = 0             # 输入逻辑行显示列位（用于 tab 展开）
        self._word_units: list[tuple[str, int, bool]] = []  # 未终止的词
        self._word_w = 0
        self._closed = False

    # ---- 禁拆片段匹配 ----
    def _match_atom(self, buf: str, i: int):
        for a in self._atoms:
            if buf.startswith(a, i):
                return a
        return None

    def _is_atom_prefix(self, buf: str, i: int) -> bool:
        # buf[i:] 是否为某禁拆片段的真前缀（不切片，逐字符比较）
        rest = len(buf) - i
        if rest >= self._max_atom_len:
            return False  # 尾部不短于最长片段，不可能是真前缀
        for a in self._atoms:
            if len(a) > rest and a.startswith(buf[i:]):
                return True
        return False

    # ---- 词事件 ----
    def _flush_word(self, final: bool):
        if self._word_units:
            self._b.word(self._word_units, final)
            self._word_units = []
            self._word_w = 0

    def _push_unit(self, text: str, w: int, atomic: bool):
        self._word_units.append((text, w, atomic))
        self._word_w += w
        self._col += w
        if self._word_w > self._width:
            # 词已确定超宽：断点只取决于已到达的簇，可提前下发（内存有界）
            self._flush_word(final=False)

    # ---- 主循环 ----
    def _process(self, final: bool):
        buf = self._buf
        n = len(buf)
        i = 0
        while i < n:
            if self._atoms:
                m = self._match_atom(buf, i)
                if m is not None:
                    self._push_unit(m, display_width(m), True)
                    i += len(m)
                    continue
                if not final and self._is_atom_prefix(buf, i):
                    break  # 尾部可能是禁拆片段前缀，等待更多数据
            ch = buf[i]
            if ch == " ":
                j = i + 1
                while j < n and buf[j] == " ":
                    if self._atoms and (
                        self._match_atom(buf, j) is not None
                        or (not final and self._is_atom_prefix(buf, j))
                    ):
                        break
                    j += 1
                self._flush_word(final=True)
                run = buf[i:j]
                self._b.spaces(run, len(run))
                self._col += len(run)
                i = j
            elif ch == "\n":
                self._flush_word(final=True)
                self._b.newline()
                self._col = 0
                i += 1
            elif ch == "\r":
                i += 1  # 忽略回车
            elif ch == "\t":
                if self._tab == "reject":
                    raise ValueError("文本包含制表符（tab='reject'）")
                self._flush_word(final=True)
                nsp = self._tabsize - (self._col % self._tabsize)
                self._b.spaces(" " * nsp, nsp)
                self._col += nsp
                i += 1
            else:
                cl, j = _next_cluster(buf, i)
                if j == n and not final:
                    break  # 簇可能未完整（后续可能有组合标记/ZWJ），等待
                self._push_unit(cl, cluster_width(cl), False)
                i = j
        self._buf = buf[i:]

    def _drain(self) -> list[str]:
        out, self._b.lines = self._b.lines, []
        return out

    def feed(self, chunk: str) -> list[str]:
        """喂入一块文本，返回本次新完成的行。"""
        if self._closed:
            raise ValueError("finish() 之后不能再 feed()")
        if not isinstance(chunk, str):
            raise TypeError("chunk 必须是 str")
        self._buf += chunk
        self._process(final=False)
        return self._drain()

    def finish(self) -> list[str]:
        """结束输入，返回剩余的行。"""
        self._closed = True
        self._process(final=True)
        self._flush_word(final=True)
        self._b.finish()
        return self._drain()


def wrap(text: str, width: int, *, tab: str = "expand", tabsize: int = 8,
         atoms=()) -> list[str]:
    """一次性按显示宽度折行，返回行列表（不含换行符）。空文本返回 []。"""
    w = Wrapper(width, tab=tab, tabsize=tabsize, atoms=atoms)
    lines = w.feed(text)
    lines.extend(w.finish())
    return lines


def truncate(text: str, width: int, *, ellipsis: str = "…",
             tab: str = "expand", tabsize: int = 8) -> str:
    """截断到指定显示宽度并追加省略标记（省略标记计入宽度）。

    降级规则：若 width 小于省略标记自身宽度，返回空串 ""。
    按单行处理：首个换行符之后的内容忽略。绝不拆散簇。
    """
    if width < 0:
        raise ValueError("width 必须 >= 0")
    if tab not in ("expand", "reject"):
        raise ValueError("tab 必须是 'expand' 或 'reject'")
    ew = display_width(ellipsis)
    if width < ew:
        return ""  # 降级：可用宽度放不下省略标记
    text = text.split("\n", 1)[0]
    if "\t" in text:
        if tab == "reject":
            raise ValueError("文本包含制表符（tab='reject'）")
        text = expand_tabs(text, tabsize)
    if display_width(text) <= width:
        return text
    budget = width - ew
    out = []
    used = 0
    for cl in clusters(text):
        w = cluster_width(cl)
        if used + w > budget:
            break
        out.append(cl)
        used += w
    return "".join(out) + ellipsis
