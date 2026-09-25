"""拼写纠错库：Trie + 编辑距离自动机剪枝检索（仅标准库）。

核心思想
--------
词表建成一棵 Trie。检索时为查询词维护编辑距离 DP 的一行：
对 Trie 上每个前缀计算完整 DP 行（Steve Hanov 的 Levenshtein-on-trie
算法），若该行所有格子都大于阈值，则以该节点为根的整棵子树不可能产生
候选，直接剪枝。支持可选相邻换位（Optimal String Alignment 版
Damerau-Levenshtein），换位列需要用到祖父层 DP 行，遍历时一并下传。

为了支撑百万词表且只用标准库，Trie 不用嵌套 dict（Python 对象开销过大），
而是用 array 压缩成三个全局数组：每个节点的子节点在数组中连续、按字符
有序存放，查找子节点用二分（bisect）。
"""

from __future__ import annotations

import heapq
from array import array
from bisect import bisect_left
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple, Union

__all__ = ["damerau_distance", "SpellChecker", "Suggestion"]


# ---------------------------------------------------------------------------
# 独立的编辑距离函数（OSA Damerau-Levenshtein / 普通 Levenshtein）
# ---------------------------------------------------------------------------

def damerau_distance(a: str, b: str, transpositions: bool = True) -> int:
    """计算两个字符串的编辑距离。

    transpositions=True 时使用 Optimal String Alignment（OSA）版
    Damerau-Levenshtein，额外允许相邻两字符换位，代价为 1；
    False 时为普通 Levenshtein（插入/删除/替换，各代价 1）。
    Trie 检索与本函数使用完全一致的递推定义。
    """
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev2: Optional[List[int]] = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (
                transpositions
                and i > 1
                and j > 1
                and ai == b[j - 2]
                and a[i - 2] == b[j - 1]
            ):
                value = min(value, prev2[j - 2] + 1)  # type: ignore[index]
            cur[j] = value
        prev2, prev = prev, cur
    return prev[lb]


@dataclass(frozen=True)
class Suggestion:
    """单条纠错候选。"""

    word: str
    distance: int
    frequency: int

    def __iter__(self):  # 方便解包 (word, distance, frequency)
        yield self.word
        yield self.distance
        yield self.frequency


# ---------------------------------------------------------------------------
# 紧凑 Trie
# ---------------------------------------------------------------------------

class _Trie:
    """压缩存储的 Trie。

    不变量：
      - 每个节点的子节点在 _chars / _targets 中占据连续区间且按字符升序；
      - 区间起点与长度由 _first[node] / _degree[node] 给出；
      - _term[node] 非 0 表示该节点是一个词的终点；
      - _freq[node] 保存词频（非终点为 0）。

    输入必须是已排序且已去重合并的词。构造利用“输入有序”的性质：
    每个节点的子节点按字母序连续到达，节点关闭时一次性刷入数组，
    构造峰值内存与最终索引同阶，不产生嵌套 dict。
    """

    __slots__ = (
        "_chars", "_targets", "_first", "_degree", "_term", "_freq",
        "_depth", "_min_term", "_max_term", "size",
    )

    def __init__(self, items: Sequence[Tuple[str, int]]):
        chars = array("i")
        targets = array("i")
        first = array("i", [0])
        degree = array("i", [0])
        term = bytearray([0])
        freq = array("q", [0])
        depths = array("i", [0])

        stack_nodes: List[int] = [0]       # 深度 d 上的节点 id（0 为根）
        stack_kids: List[List[Tuple[int, int]]] = [[]]  # 各节点待刷入的子节点

        def close(d: int) -> None:
            node = stack_nodes[d]
            kids = stack_kids[d]
            first[node] = len(chars)
            degree[node] = len(kids)
            for code, child in kids:
                chars.append(code)
                targets.append(child)
            stack_nodes.pop()
            stack_kids.pop()

        previous = ""
        for word, word_freq in items:
            limit = min(len(word), len(previous))
            common = 0
            while common < limit and word[common] == previous[common]:
                common += 1
            # 公共前缀以下的旧分支全部关闭
            for d in range(len(stack_nodes) - 1, common, -1):
                close(d)
            # 沿新后缀建节点
            for pos in range(common, len(word)):
                child = len(term)
                first.append(0)
                degree.append(0)
                term.append(0)
                freq.append(0)
                depths.append(len(stack_nodes))
                stack_kids[len(stack_nodes) - 1].append((ord(word[pos]), child))
                stack_nodes.append(child)
                stack_kids.append([])
            leaf = stack_nodes[-1]
            term[leaf] = 1
            freq[leaf] = word_freq
            previous = word

        for d in range(len(stack_nodes) - 1, -1, -1):
            close(d)

        # 子树终点词长上下界（节点 id 为先序分配，子 id 均大于父 id，
        # 反向扫描即后序归约）；检索时用于按“词长差 <= 阈值”剪整棵子树。
        size = len(term)
        INF = 0x7FFFFFFF
        min_term = array("i", [INF]) * size
        max_term = array("i", [-1]) * size
        for node in range(size - 1, -1, -1):
            lo = first[node]
            hi = lo + degree[node]
            if term[node]:
                lo_d = hi_d = depths[node]
            else:
                lo_d, hi_d = INF, -1
            for idx in range(lo, hi):
                child = targets[idx]
                cmin = min_term[child]
                if cmin < lo_d:
                    lo_d = cmin
                cmax = max_term[child]
                if cmax > hi_d:
                    hi_d = cmax
            min_term[node] = lo_d
            max_term[node] = hi_d

        self._chars = chars
        self._targets = targets
        self._first = first
        self._degree = degree
        self._term = term
        self._freq = freq
        self._depth = depths
        self._min_term = min_term
        self._max_term = max_term
        self.size = size

    def child(self, node: int, code: int) -> int:
        """字符 -> 子节点 id，不存在返回 -1（构建/测试用）。"""
        lo = self._first[node]
        hi = lo + self._degree[node]
        idx = bisect_left(self._chars, code, lo, hi)
        if idx < hi and self._chars[idx] == code:
            return self._targets[idx]
        return -1


# ---------------------------------------------------------------------------
# 拼写纠错器
# ---------------------------------------------------------------------------

Entry = Union[str, Tuple[str, int]]


class SpellChecker:
    """Trie + 编辑距离自动机剪枝的拼写纠错器。

    边界策略（均可通过子类覆盖）：
      - MAX_DISTANCE_LIMIT = 3：阈值超过 3 一律拒绝（自动机剪枝在大阈值下
        失效，候选集指数膨胀，属可预见的性能陷阱）；
      - MAX_QUERY_LENGTH = 128：超长查询拒绝；
      - 空查询返回空候选；空词表返回空候选；
      - 重复词合并为一个词条，词频累加（可解释、确定）。
    """

    MAX_DISTANCE_LIMIT = 3
    MAX_QUERY_LENGTH = 128

    def __init__(self, entries: Iterable[Entry] = ()):
        merged = {}
        add = merged.__setitem__
        get = merged.get
        for entry in entries:
            if isinstance(entry, str):
                word, word_freq = entry, 1
            else:
                word, word_freq = entry
                word_freq = int(word_freq)
            if word == "":
                raise ValueError("词表中不允许空字符串")
            if word_freq < 0:
                raise ValueError(f"词频不能为负: {word!r}")
            add(word, get(word, 0) + word_freq)
        self._frequencies = merged
        self._trie: Optional[_Trie] = None
        self._build_stats: Tuple[int, int] = (len(merged), 0)
        if merged:
            self._build()

    # -- 构造 ---------------------------------------------------------------

    def _build(self) -> None:
        items = sorted(self._frequencies.items())
        trie = _Trie(items)
        self._trie = trie
        self._build_stats = (len(items), sum(len(w) for w, _ in items))

    @property
    def vocab_size(self) -> int:
        return len(self._frequencies)

    @property
    def trie_nodes(self) -> int:
        return self._trie.size if self._trie is not None else 1

    # -- 检索 ---------------------------------------------------------------

    def _validate(self, query: str, max_distance: int) -> None:
        if not isinstance(query, str):
            raise TypeError("query 必须是 str")
        if max_distance < 0:
            raise ValueError(f"max_distance 不能为负: {max_distance}")
        if max_distance > self.MAX_DISTANCE_LIMIT:
            raise ValueError(
                f"max_distance={max_distance} 超过上限 "
                f"{self.MAX_DISTANCE_LIMIT}：自动机剪枝在大阈值下失效、"
                "候选集指数膨胀，请缩小阈值或改用其他检索策略"
            )
        if len(query) > self.MAX_QUERY_LENGTH:
            raise ValueError(
                f"查询长度 {len(query)} 超过上限 {self.MAX_QUERY_LENGTH}"
            )

    def _collect(
        self, query: str, max_distance: int, transpositions: bool
    ) -> List[Tuple[str, int, int]]:
        """遍历 Trie，返回所有 distance <= max_distance 的 (词, 距离, 词频)。

        两层剪枝：
        1. DP 行最小值 > 阈值的节点，其子树不可能出候选（自动机剪枝）；
        2. 子树内终点词长不在 [|q|-d, |q|+d] 内的子树整棵跳过。
        一个记忆化：相同 (父DP行, 祖父DP行, 父字符, 本字符) 的转移结果
        相同（即同一个 Levenshtein 自动机状态），缓存 DP 行，避免对
        Trie 中大量重复状态重复递推。
        """
        if not query or not self._frequencies:
            return []
        trie = self._trie
        n = len(query)
        results: List[Tuple[str, int, int]] = []
        chars = trie._chars
        targets = trie._targets
        first = trie._first
        degree = trie._degree
        term = trie._term
        freq = trie._freq
        min_term = trie._min_term
        max_term = trie._max_term
        qcodes = [ord(ch) for ch in query]
        low_len = n - max_distance
        high_len = n + max_distance

        # 仅当 (code, prev_code) 是查询中某对相邻字符时，换位分支才可能
        # 影响 DP 值——此时转移才依赖祖父行；其余情形键中祖父行退化为 0，
        # 大幅提高不同 Trie 分支之间的缓存命中率。
        swap_pairs = set()
        if transpositions and n >= 2:
            for i in range(n - 1):
                swap_pairs.add((qcodes[i], qcodes[i + 1]))

        # 两级缓存：prev_row(自动机状态) -> {(prev_code, code, grand)} -> 结果
        transition_cache: dict = {}
        no_grand = 0

        def transition(prev_row, grand_row, code, prev_code):
            inner = transition_cache.get(prev_row)
            if inner is None:
                inner = {}
                transition_cache[prev_row] = inner
            grand_key = (
                grand_row
                if grand_row is not None and (code, prev_code) in swap_pairs
                else no_grand
            )
            key = (prev_code, code, grand_key)
            cached = inner.get(key)
            if cached is not None:
                return cached
            row = [prev_row[0] + 1]
            row_append = row.append
            swap_enabled = grand_key != no_grand
            for i in range(1, n + 1):
                cost = 0 if qcodes[i - 1] == code else 1
                value = prev_row[i] + 1
                candidate = row[i - 1] + 1
                if candidate < value:
                    value = candidate
                candidate = prev_row[i - 1] + cost
                if candidate < value:
                    value = candidate
                if swap_enabled and i >= 2:
                    if (
                        qcodes[i - 1] == prev_code
                        and qcodes[i - 2] == code
                    ):
                        candidate = grand_row[i - 2] + 1
                        if candidate < value:
                            value = candidate
                row_append(value)
            min_cell = row[0]
            for cell in row[1:]:
                if cell < min_cell:
                    min_cell = cell
            packed = (tuple(row), min_cell)
            inner[key] = packed
            return packed

        path: List[int] = []

        def walk(node, code, prev_row, grand_row, prev_code):
            row, row_min = transition(prev_row, grand_row, code, prev_code)
            if row_min > max_distance:
                return
            path.append(code)
            if term[node] and row[n] <= max_distance:
                results.append(
                    ("".join(map(chr, path)), row[n], freq[node])
                )
            lo = first[node]
            hi = lo + degree[node]
            for idx in range(lo, hi):
                child = targets[idx]
                cmax = max_term[child]
                if cmax < low_len:
                    continue
                if min_term[child] > high_len:
                    continue
                walk(child, chars[idx], row, prev_row, code)
            path.pop()

        root_row = tuple(range(n + 1))
        lo = first[0]
        hi = lo + degree[0]
        for idx in range(lo, hi):
            child = targets[idx]
            if max_term[child] < low_len or min_term[child] > high_len:
                continue
            walk(child, chars[idx], root_row, None, -1)
        return results

    @staticmethod
    def _rank_key(query: str, candidate: Tuple[str, int, int]):
        """确定性排序键（全部参与比较，无隐式并列）：

        1. 编辑距离（越小越好）；
        2. 词频（越高越好）；
        3. 与查询的长度差（越小越好）；
        4. 词形字典序（最终确定性决胜，保证跨平台一致）。
        """
        word, dist, word_freq = candidate
        return (dist, -word_freq, abs(len(word) - len(query)), word)

    def candidates(
        self,
        query: str,
        max_distance: int = 2,
        transpositions: bool = True,
    ) -> List[Suggestion]:
        """返回阈值内的**全量**候选，按确定性排序键排序。"""
        self._validate(query, max_distance)
        raw = self._collect(query, max_distance, transpositions)
        raw.sort(key=lambda item: self._rank_key(query, item))
        return [Suggestion(*item) for item in raw]

    def suggest(
        self,
        query: str,
        k: int = 10,
        max_distance: int = 2,
        transpositions: bool = True,
    ) -> List[Suggestion]:
        """返回 Top-K。

        实现上先由自动机剪枝得到阈值内**全部**候选，再用
        heapq.nsmallest 按与全量排序相同的键取前 K —— 因此
        ``suggest(..., k)`` 恒等于 ``candidates(...)[:k]``，
        不存在按 k 提前剪枝带来的不一致（有测试强制验证）。
        """
        self._validate(query, max_distance)
        if k <= 0:
            return []
        raw = self._collect(query, max_distance, transpositions)
        top = heapq.nsmallest(
            k, raw, key=lambda item: self._rank_key(query, item)
        )
        return [Suggestion(*item) for item in top]
