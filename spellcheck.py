"""spellcheck — 纯标准库拼写纠错库。

核心设计：
- 词表构建为扁平数组 Trie（edge_lo / edge_ch / edge_to / freq 四个并行数组），
  内存紧凑，支持百万级词表。
- 检索使用 Levenshtein 自动机（以 DP 列向量为状态的 NFA，列值截断到 d+1）
  与 Trie 做交积遍历：只访问“存在某个 query 前缀与其编辑距离 <= d”的 Trie 节点，
  其余子树整体剪掉，绝不对全词表逐个算距离。
- 换位（相邻字符交换）为可选特性，使用 OSA（Optimal String Alignment）版本的
  自动机状态（额外携带上一列与上一个字符）。
- 候选排序键：(编辑距离, -词频, 长度差, 字典序)，为全序，结果确定；
  Top-K 用堆选取，与全量排序取前 K 完全一致。

限制（超出即拒绝，抛 ValueError）：
- max_distance 上限 3（阈值再大自动机状态爆炸，交互场景无意义）。
- query 长度上限 64。
"""

from __future__ import annotations

import heapq
from array import array
from collections import namedtuple

__all__ = [
    "Suggestion",
    "SpellCorrector",
    "levenshtein_distance",
    "damerau_distance",
    "MAX_EDIT_DISTANCE",
    "MAX_QUERY_LENGTH",
]

MAX_EDIT_DISTANCE = 3
MAX_QUERY_LENGTH = 64

Suggestion = namedtuple("Suggestion", ["word", "distance", "frequency"])


# ---------------------------------------------------------------------------
# 编辑距离（带可选阈值的 banded DP）
# ---------------------------------------------------------------------------

def _bounded_distance(a, b, max_distance, transpositions):
    """返回编辑距离；若超过 max_distance 返回 None。banded DP，O(d*min(len))。"""
    la, lb = len(a), len(b)
    if max_distance is None:
        max_distance = max(la, lb)
    if abs(la - lb) > max_distance:
        return None
    if la == 0:
        return lb if lb <= max_distance else None
    if lb == 0:
        return la if la <= max_distance else None

    INF = max_distance + 1
    band = min(lb, max_distance)
    prev = list(range(band + 1)) + [INF] * (lb - band)
    prev_prev = None
    for i in range(1, la + 1):
        lo = max(1, i - max_distance)
        hi = min(lb, i + max_distance)
        cur = [INF] * (lb + 1)
        cur[0] = i if i <= max_distance else INF
        ai = a[i - 1]
        ai_prev = a[i - 2] if i >= 2 else None
        for j in range(lo, hi + 1):
            cost = 0 if ai == b[j - 1] else 1
            v = prev[j] + 1
            u = cur[j - 1] + 1
            if u < v:
                v = u
            u = prev[j - 1] + cost
            if u < v:
                v = u
            if transpositions and i >= 2 and j >= 2 \
                    and ai == b[j - 2] and ai_prev == b[j - 1]:
                u = prev_prev[j - 2] + 1
                if u < v:
                    v = u
            cur[j] = v if v <= max_distance else INF
        prev_prev, prev = prev, cur
    d = prev[lb]
    return d if d <= max_distance else None


def levenshtein_distance(a, b, max_distance=None):
    """Levenshtein 距离（插入/删除/替换各代价 1）。

    max_distance 给定时做带状剪枝，距离超过阈值返回 None。
    """
    return _bounded_distance(a, b, max_distance, False)


def damerau_distance(a, b, max_distance=None):
    """OSA 距离：在 Levenshtein 基础上允许相邻字符换位（代价 1）。

    max_distance 给定时做带状剪枝，距离超过阈值返回 None。
    """
    return _bounded_distance(a, b, max_distance, True)


# ---------------------------------------------------------------------------
# 自动机状态转移（列向量 NFA，列值截断到 cap = d+1；截断不影响 <= d 的精确性）
# ---------------------------------------------------------------------------

def _step_lev(col, c, qc, m, cap):
    n0 = col[0] + 1
    if n0 > cap:
        n0 = cap
    new = [n0]
    app = new.append
    for i in range(1, m + 1):
        cost = 0 if qc[i - 1] == c else 1
        v = col[i] + 1
        u = new[i - 1] + 1
        if u < v:
            v = u
        u = col[i - 1] + cost
        if u < v:
            v = u
        if v > cap:
            v = cap
        app(v)
    return tuple(new)


def _step_osa(state, c, qc, m, cap):
    pp, p, last = state
    n0 = p[0] + 1
    if n0 > cap:
        n0 = cap
    new = [n0]
    app = new.append
    for i in range(1, m + 1):
        cost = 0 if qc[i - 1] == c else 1
        v = p[i] + 1
        u = new[i - 1] + 1
        if u < v:
            v = u
        u = p[i - 1] + cost
        if u < v:
            v = u
        if i >= 2 and pp is not None and qc[i - 1] == last and qc[i - 2] == c:
            u = pp[i - 2] + 1
            if u < v:
                v = u
        if v > cap:
            v = cap
        app(v)
    return (p, tuple(new), c)


# ---------------------------------------------------------------------------
# 纠错器：Trie 索引 + 自动机检索 + 排序
# ---------------------------------------------------------------------------

class SpellCorrector:
    """用法::

        sc = SpellCorrector([("apple", 100), ("apply", 10), "ape"])
        sc.suggest("appl", max_distance=1, top_k=5)

    words 元素可以是 str（词频计 1）或 (word, freq) 对；重复词合并、词频累加。
    """

    def __init__(self, words=(), *, max_edit_distance=MAX_EDIT_DISTANCE,
                 max_query_length=MAX_QUERY_LENGTH):
        if not (1 <= max_edit_distance <= MAX_EDIT_DISTANCE):
            raise ValueError(
                f"max_edit_distance 须在 1..{MAX_EDIT_DISTANCE} 之间")
        self.max_edit_distance = max_edit_distance
        self.max_query_length = max_query_length

        counts = {}
        for item in words:
            if isinstance(item, str):
                w, f = item, 1
            else:
                w, f = item
                f = int(f)
            if not w:
                continue
            counts[w] = counts.get(w, 0) + f
        self._vocab = sorted(counts)
        freqs = [counts[w] for w in self._vocab]
        self._build_trie(self._vocab, freqs)

    def __len__(self):
        return len(self._vocab)

    # -- Trie 构建（两遍扫描有序词表，直接生成扁平数组） --

    def _build_trie(self, words, freqs):
        # 第一遍：统计每个节点的子节点数，并记录终止节点词频
        child_count = array("i", [0])
        freq = array("q", [0])
        stack = [0]
        prev = ""
        for w, f in zip(words, freqs):
            cpl = 0
            for x, y in zip(prev, w):
                if x != y:
                    break
                cpl += 1
            del stack[cpl + 1:]
            node = stack[-1]
            for _ in w[cpl:]:
                child_count[node] += 1
                child_count.append(0)
                freq.append(0)
                node = len(child_count) - 1
                stack.append(node)
            freq[node] = f
            prev = w

        n = len(child_count)
        edge_lo = array("i", [0]) * (n + 1)
        acc = 0
        for i in range(n):
            acc += child_count[i]
            edge_lo[i + 1] = acc

        # 第二遍：按相同顺序重放，填充边表（同一节点的边按字符有序）
        edge_ch = array("i", [0]) * acc
        edge_to = array("i", [0]) * acc
        cursor = array("i", edge_lo[:-1])
        stack = [0]
        prev = ""
        next_id = 1
        for w in words:
            cpl = 0
            for x, y in zip(prev, w):
                if x != y:
                    break
                cpl += 1
            del stack[cpl + 1:]
            node = stack[-1]
            for ch in w[cpl:]:
                s = cursor[node]
                cursor[node] = s + 1
                edge_ch[s] = ord(ch)
                edge_to[s] = next_id
                node = next_id
                next_id += 1
                stack.append(node)
            prev = w

        self._edge_lo = edge_lo
        self._edge_ch = edge_ch
        self._edge_to = edge_to
        self._freq = freq
        self._node_count = n

    # -- 自动机 × Trie 交积遍历 --

    def _collect(self, query, d, transpositions):
        """返回 [(word, distance, freq)]，即所有距离 <= d 的词。"""
        qc = [ord(c) for c in query]
        m = len(qc)
        cap = d + 1
        init = tuple(i if i <= d else cap for i in range(m + 1))
        edge_lo = self._edge_lo
        edge_ch = self._edge_ch
        edge_to = self._edge_to
        freq = self._freq
        results = []
        cache = {}
        if transpositions:
            start = (None, init, -1)  # (上上列, 上一列, 上一字符)
            step = _step_osa
        else:
            start = init
            step = _step_lev
        # 栈元素: (节点, 自动机状态, 父节点标签长度, 本节点字符)
        stack = [(0, start, 0, "")]
        path = []
        push = stack.append
        pop = stack.pop
        while stack:
            node, state, plen, ch = pop()
            del path[plen:]
            if ch:
                path.append(ch)
            col = state[1] if transpositions else state
            f = freq[node]
            if f and col[m] <= d:
                results.append(("".join(path), col[m], f))
            if min(col) > d:  # 该子树不可能产生 <= d 的匹配，整体剪掉
                continue
            for slot in range(edge_lo[node], edge_lo[node + 1]):
                c = edge_ch[slot]
                key = (state, c)
                ns = cache.get(key)
                if ns is None:
                    ns = step(state, c, qc, m, cap)
                    cache[key] = ns
                ncol = ns[1] if transpositions else ns
                if min(ncol) <= d:
                    push((edge_to[slot], ns, len(path), chr(c)))
        return results

    # -- 对外接口 --

    def _validate(self, query, max_distance):
        if not isinstance(query, str):
            raise TypeError("query 必须是 str")
        if not isinstance(max_distance, int) or isinstance(max_distance, bool):
            raise TypeError("max_distance 必须是 int")
        if max_distance < 0:
            raise ValueError("max_distance 不能为负")
        if max_distance > self.max_edit_distance:
            raise ValueError(
                f"max_distance={max_distance} 超过本库上限 {self.max_edit_distance}，"
                f"已拒绝（阈值过大会导致候选爆炸，请改用 <= {self.max_edit_distance} 的阈值）")
        if len(query) > self.max_query_length:
            raise ValueError(
                f"query 长度 {len(query)} 超过上限 {self.max_query_length}，已拒绝")

    def suggest(self, query, *, max_distance=2, top_k=10, transpositions=False):
        """返回 List[Suggestion]，按 (距离, -词频, 长度差, 字典序) 升序。

        - 空 query 或空词表：返回 []。
        - max_distance 超过上限 / query 超长：抛 ValueError（拒绝策略）。
        - top_k=None 返回全量排序结果；否则用堆取前 K，
          与全量排序后取前 K 完全一致（排序键为全序）。
        """
        self._validate(query, max_distance)
        if not query or not self._vocab:
            return []
        if top_k is not None and top_k <= 0:
            return []
        cands = self._collect(query, max_distance, transpositions)
        m = len(query)
        ranked = [(dist, -f, abs(len(w) - m), w, f) for w, dist, f in cands]
        if top_k is None:
            ranked.sort()
            chosen = ranked
        else:
            chosen = heapq.nsmallest(top_k, ranked)
        return [Suggestion(w, dist, f) for dist, _, _, w, f in chosen]


if __name__ == "__main__":  # 简单演示
    demo = SpellCorrector([
        ("apple", 100), ("apply", 60), ("ape", 5), ("app", 30),
        ("banana", 80), ("band", 20), ("banner", 10),
    ])
    for s in demo.suggest("appel", max_distance=2, top_k=5):
        print(s)
