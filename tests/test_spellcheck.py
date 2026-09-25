"""spellcheck 自测：边界条件、暴力枚举等价性、Top-K 一致性、确定性。"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from spellcheck import SpellChecker, Suggestion, damerau_distance


def brute_force(vocab, query, max_distance, transpositions):
    """朴素实现：对整张词表逐个算距离，作为正确性基准。"""
    out = []
    for word, freq in vocab.items():
        d = damerau_distance(query, word, transpositions)
        if d <= max_distance:
            out.append((word, d, freq))
    out.sort(
        key=lambda item: (
            item[1],
            -item[2],
            abs(len(item[0]) - len(query)),
            item[0],
        )
    )
    return [Suggestion(*item) for item in out]


class TestDistance(unittest.TestCase):
    def test_basic_levenshtein(self):
        self.assertEqual(damerau_distance("kitten", "sitting", False), 3)
        self.assertEqual(damerau_distance("saturday", "sunday", False), 3)
        self.assertEqual(damerau_distance("abc", "abc", False), 0)
        self.assertEqual(damerau_distance("", "abc", False), 3)
        self.assertEqual(damerau_distance("abc", "", False), 3)

    def test_transposition(self):
        # 相邻换位代价为 1（OSA）
        self.assertEqual(damerau_distance("teh", "the", True), 1)
        self.assertEqual(damerau_distance("teh", "the", False), 2)
        self.assertEqual(damerau_distance("ab", "ba", True), 1)
        # OSA 的已知限制：每个子串只允许编辑一次，故 CA->ABC 为 3
        # （严格 Damerau 可借助换位+插入做到 2，但 OSA 不允许重复编辑）
        self.assertEqual(damerau_distance("CA", "ABC", True), 3)


class TestSearchEquivalence(unittest.TestCase):
    """自动机剪枝结果必须与逐个计算的暴力结果完全一致。"""

    def setUp(self):
        random.seed(20260925)
        words = set()
        while len(words) < 400:
            length = random.randint(2, 8)
            words.add("".join(random.choice("abcde") for _ in range(length)))
        self.vocab = {w: random.randint(1, 1000) for w in words}

    def test_all_queries(self):
        checker = SpellChecker(self.vocab.items())
        # 注：空查询由边界用例单独约定（返回 []）
        queries = ["a", "ab", "abc", "abcd", "bce", "xxxxx", "eeeeee"]
        queries += random.sample(sorted(self.vocab), 25)
        for trans in (True, False):
            for dist in range(0, 4):
                for query in queries:
                    got = checker.candidates(query, dist, trans)
                    want = brute_force(self.vocab, query, dist, trans)
                    self.assertEqual(
                        got,
                        want,
                        msg=f"query={query!r} d={dist} trans={trans}",
                    )


class TestTopKConsistency(unittest.TestCase):
    def setUp(self):
        random.seed(7)
        words = set()
        while len(words) < 800:
            length = random.randint(2, 9)
            words.add("".join(random.choice("abcdef") for _ in range(length)))
        vocab = {w: random.randint(1, 50) for w in words}  # 刻意制造大量并列
        self.checker = SpellChecker(vocab.items())
        self.queries = random.sample(sorted(vocab), 40) + ["aaaa", "zzzzz"]

    def test_topk_equals_full_sort_prefix(self):
        for trans in (True, False):
            for dist in (1, 2, 3):
                for query in self.queries:
                    full = self.checker.candidates(query, dist, trans)
                    for k in (1, 2, 5, 10, 50, 10_000):
                        top = self.checker.suggest(query, k, dist, trans)
                        self.assertEqual(
                            top,
                            full[:k],
                            msg=f"q={query!r} k={k} d={dist} t={trans}",
                        )

    def test_k_non_positive(self):
        self.assertEqual(self.checker.suggest("abc", k=0), [])
        self.assertEqual(self.checker.suggest("abc", k=-3), [])

    def test_deterministic_across_calls(self):
        first = self.checker.suggest("aaaa", k=10, max_distance=3)
        for _ in range(5):
            self.assertEqual(
                self.checker.suggest("aaaa", k=10, max_distance=3), first
            )


class TestEdgeCases(unittest.TestCase):
    def test_empty_vocab(self):
        checker = SpellChecker([])
        self.assertEqual(checker.candidates("hello"), [])
        self.assertEqual(checker.suggest("hello"), [])

    def test_empty_query(self):
        checker = SpellChecker(["cat", "dog"])
        self.assertEqual(checker.candidates(""), [])
        self.assertEqual(checker.suggest(""), [])

    def test_duplicate_words_merge_frequency(self):
        checker = SpellChecker(
            [("cat", 3), ("dog", 1), ("cat", 5), ("dog", 2)]
        )
        self.assertEqual(checker.vocab_size, 2)
        # 词频累加：cat 3+5=8，dog 1+2=3；同距离时词频高者排前
        got = {s.word: s.frequency for s in checker.candidates("zzz", 3)}
        self.assertEqual(got, {"cat": 8, "dog": 3})
        # "xag" 与 cat、dog 的距离均为 2，词频高的 cat 必须排前
        ranking = checker.suggest("xag", k=10, max_distance=2)
        words = [s.word for s in ranking]
        self.assertEqual(words, ["cat", "dog"])

    def test_threshold_too_large_rejected(self):
        checker = SpellChecker(["cat"])
        with self.assertRaises(ValueError):
            checker.suggest("cat", max_distance=4)
        with self.assertRaises(ValueError):
            checker.candidates("cat", max_distance=99)

    def test_negative_threshold_rejected(self):
        checker = SpellChecker(["cat"])
        with self.assertRaises(ValueError):
            checker.candidates("cat", max_distance=-1)

    def test_overlong_query_rejected(self):
        checker = SpellChecker(["cat"])
        with self.assertRaises(ValueError):
            checker.candidates("x" * (SpellChecker.MAX_QUERY_LENGTH + 1))
        # 恰好在边界上应当通过
        checker.candidates("x" * SpellChecker.MAX_QUERY_LENGTH, max_distance=3)

    def test_invalid_entries(self):
        with self.assertRaises(ValueError):
            SpellChecker([""])
        with self.assertRaises(ValueError):
            SpellChecker([("cat", -1)])

    def test_zero_distance_exact_match(self):
        checker = SpellChecker(["apple", "apply"])
        self.assertEqual(
            checker.candidates("apple", max_distance=0),
            [Suggestion("apple", 0, 1)],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
