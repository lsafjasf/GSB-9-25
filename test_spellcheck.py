"""spellcheck 自测：距离函数、边界情况、Top-K 一致性、暴力对拍。"""

import random
import unittest

from spellcheck import (
    SpellCorrector,
    damerau_distance,
    levenshtein_distance,
)


class TestDistance(unittest.TestCase):
    def test_levenshtein_basic(self):
        self.assertEqual(levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(levenshtein_distance("", ""), 0)
        self.assertEqual(levenshtein_distance("", "abc"), 3)
        self.assertEqual(levenshtein_distance("abc", "abc"), 0)
        self.assertEqual(levenshtein_distance("abc", "abd"), 1)
        self.assertEqual(levenshtein_distance("abc", "abcd"), 1)
        self.assertEqual(levenshtein_distance("flaw", "lawn"), 2)

    def test_transposition_optional(self):
        # 不允许换位：ab -> ba 需要 2 次替换
        self.assertEqual(levenshtein_distance("ab", "ba"), 2)
        # 允许换位（OSA）：1 次换位
        self.assertEqual(damerau_distance("ab", "ba"), 1)
        self.assertEqual(damerau_distance("abcd", "abdc"), 1)
        self.assertEqual(damerau_distance("apple", "appel"), 1)

    def test_max_distance_cutoff(self):
        self.assertIsNone(levenshtein_distance("kitten", "sitting", 2))
        self.assertEqual(levenshtein_distance("kitten", "sitting", 3), 3)
        self.assertIsNone(damerau_distance("abcdef", "xyz", 3))
        self.assertEqual(levenshtein_distance("abc", "abc", 0), 0)
        self.assertIsNone(levenshtein_distance("abc", "abd", 0))

    def test_distance_matches_reference(self):
        # 与无剪枝的朴素 DP 对拍
        def ref(a, b):
            m, n = len(a), len(b)
            dp = [[0] * (n + 1) for _ in range(m + 1)]
            for i in range(m + 1):
                dp[i][0] = i
            for j in range(n + 1):
                dp[0][j] = j
            for i in range(1, m + 1):
                for j in range(1, n + 1):
                    dp[i][j] = min(
                        dp[i - 1][j] + 1,
                        dp[i][j - 1] + 1,
                        dp[i - 1][j - 1] + (a[i - 1] != b[j - 1]),
                    )
            return dp[m][n]

        rng = random.Random(7)
        for _ in range(300):
            a = "".join(rng.choices("abc", k=rng.randint(0, 8)))
            b = "".join(rng.choices("abc", k=rng.randint(0, 8)))
            d = ref(a, b)
            self.assertEqual(levenshtein_distance(a, b), d)
            for cap in range(0, 5):
                got = levenshtein_distance(a, b, cap)
                self.assertEqual(got, d if d <= cap else None, (a, b, cap))


class TestEdgeCases(unittest.TestCase):
    def test_empty_query(self):
        sc = SpellCorrector(["apple", "ape"])
        self.assertEqual(sc.suggest(""), [])

    def test_empty_vocab(self):
        sc = SpellCorrector([])
        self.assertEqual(len(sc), 0)
        self.assertEqual(sc.suggest("apple", max_distance=2), [])

    def test_duplicate_words_merged(self):
        sc = SpellCorrector(["apple", "apple", ("apple", 2), "ape"])
        self.assertEqual(len(sc), 2)
        hits = sc.suggest("apple", max_distance=0)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].word, "apple")
        self.assertEqual(hits[0].frequency, 4)  # 1 + 1 + 2

    def test_threshold_too_large_rejected(self):
        sc = SpellCorrector(["apple"])
        with self.assertRaises(ValueError):
            sc.suggest("apple", max_distance=4)
        with self.assertRaises(ValueError):
            sc.suggest("apple", max_distance=-1)

    def test_overlong_query_rejected(self):
        sc = SpellCorrector(["apple"])
        with self.assertRaises(ValueError):
            sc.suggest("a" * 65, max_distance=1)
        # 边界：恰好 64 允许
        self.assertEqual(sc.suggest("a" * 64, max_distance=1), [])

    def test_top_k_zero(self):
        sc = SpellCorrector(["apple"])
        self.assertEqual(sc.suggest("apple", max_distance=1, top_k=0), [])


class TestRanking(unittest.TestCase):
    def test_ranking_order_and_tiebreak(self):
        # 距离优先；同距离比词频；同频比长度差；再并列按字典序
        sc = SpellCorrector([
            ("apple", 1),     # dist 0
            ("appl", 200),    # dist 1，词频最高
            ("apply", 100),   # dist 1，词频 100，字典序在 appla 后
            ("appla", 100),   # dist 1，词频 100，字典序优先
            ("apples", 50),   # dist 1，词频最低
        ])
        got = [s.word for s in sc.suggest("apple", max_distance=2, top_k=None)]
        self.assertEqual(got, ["apple", "appl", "appla", "apply", "apples"])

    def test_lexicographic_final_tiebreak(self):
        sc = SpellCorrector([("ab", 1), ("aa", 1)])
        got = [s.word for s in sc.suggest("ac", max_distance=1, top_k=None)]
        self.assertEqual(got, ["aa", "ab"])

    def test_deterministic(self):
        sc = SpellCorrector([("apple", 1), ("apply", 1), ("ape", 1)])
        a = sc.suggest("appl", max_distance=2, top_k=3)
        b = sc.suggest("appl", max_distance=2, top_k=3)
        self.assertEqual(a, b)

    def test_topk_matches_full_sort(self):
        rng = random.Random(123)
        vocab = list({"".join(rng.choices("abcde", k=rng.randint(2, 7)))
                      for _ in range(3000)})
        entries = [(w, rng.randint(1, 1000)) for w in vocab]
        sc = SpellCorrector(entries)
        for _ in range(50):
            q = "".join(rng.choices("abcde", k=rng.randint(2, 7)))
            for d in (1, 2, 3):
                for tr in (False, True):
                    full = sc.suggest(q, max_distance=d, top_k=None,
                                      transpositions=tr)
                    for k in (1, 3, 10, 100):
                        top = sc.suggest(q, max_distance=d, top_k=k,
                                         transpositions=tr)
                        self.assertEqual(top, full[:k],
                                         (q, d, tr, k))


class TestAutomatonVsBruteForce(unittest.TestCase):
    """自动机检索结果与全词表暴力算距离逐一对比（小词表、小字母表保证命中率）。"""

    def test_collect_matches_brute_force(self):
        rng = random.Random(99)
        vocab = list({"".join(rng.choices("abc", k=rng.randint(1, 6)))
                      for _ in range(800)})
        sc = SpellCorrector(vocab)
        dist_fn = {False: levenshtein_distance, True: damerau_distance}
        for _ in range(60):
            q = "".join(rng.choices("abc", k=rng.randint(1, 6)))
            for d in (0, 1, 2, 3):
                for tr in (False, True):
                    expected = {}
                    fn = dist_fn[tr]
                    for w in vocab:
                        dist = fn(q, w, d)
                        if dist is not None:
                            expected[w] = dist
                    got = {w: dist for w, dist, _f in sc._collect(q, d, tr)}
                    self.assertEqual(got, expected, (q, d, tr))

    def test_transposition_search_finds_swapped(self):
        sc = SpellCorrector(["ba", "ab"])
        got = [s.word for s in sc.suggest("ab", max_distance=1,
                                          transpositions=True)]
        self.assertIn("ba", got)
        got = [s.word for s in sc.suggest("ab", max_distance=1,
                                          transpositions=False)]
        self.assertNotIn("ba", got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
