"""versioning 单元测试：全序公理断言、排序/逐对比较一致性、边界与报错位置。"""

import random
import unittest

from versioning import (
    Range,
    RangeError,
    Version,
    VersionError,
    parse_range,
    parse_version,
)

pv = parse_version
pr = parse_range


def random_version(rng: random.Random) -> str:
    core = f"{rng.randrange(4)}.{rng.randrange(4)}.{rng.randrange(4)}"
    if rng.random() < 0.5:
        idents = []
        for _ in range(rng.randrange(1, 4)):
            if rng.random() < 0.5:
                idents.append(str(rng.randrange(12)))
            else:
                idents.append(rng.choice(["a", "b", "alpha", "beta", "rc", "x-1"]))
        core += "-" + ".".join(idents)
    if rng.random() < 0.4:
        core += "+" + rng.choice(["b1", "b2", "001", "meta.x", "a"])
    return core


class TestTotalOrder(unittest.TestCase):
    """对随机版本集断言全序公理，并验证排序与逐对比较一致。"""

    def setUp(self):
        self.rng = random.Random(20260925)
        self.versions = [pv(random_version(self.rng)) for _ in range(60)]

    def test_trichotomy(self):
        for a in self.versions:
            for b in self.versions:
                outcomes = [a < b, a == b, a > b]
                self.assertEqual(sum(outcomes), 1, (a, b))

    def test_antisymmetry_and_eq(self):
        for a in self.versions:
            self.assertFalse(a < a)
            self.assertEqual(a, a)
            for b in self.versions:
                self.assertEqual(a < b, b > a)
                self.assertEqual(a == b, b == a)
                if a < b:
                    self.assertFalse(b < a)
                    self.assertNotEqual(a, b)

    def test_transitivity(self):
        for _ in range(3000):
            a, b, c = (self.rng.choice(self.versions) for _ in range(3))
            if a < b and b < c:
                self.assertLess(a, c)
            if a == b and b == c:
                self.assertEqual(a, c)

    def test_sort_matches_pairwise(self):
        """sorted() 结果必须与逐对比较的插入排序完全一致。"""
        sample = [self.rng.choice(self.versions) for _ in range(30)]

        def insertion_sort(items):
            out = []
            for x in items:
                i = 0
                while i < len(out) and not x < out[i]:
                    i += 1
                out.insert(i, x)
            return out

        self.assertEqual(sorted(sample), insertion_sort(sample))
        # 不同字符串 => 不同位次（严格全序，无“相等但不同串”）
        distinct = {str(v) for v in self.versions}
        ordered = sorted(pv(s) for s in distinct)
        for x, y in zip(ordered, ordered[1:]):
            self.assertLess(x, y)

    def test_hash_consistent_with_eq(self):
        for a in self.versions:
            for b in self.versions:
                if a == b:
                    self.assertEqual(hash(a), hash(b))


class TestCompareRules(unittest.TestCase):
    def test_core_numeric(self):
        self.assertLess(pv("1.9.9"), pv("1.10.0"))
        self.assertEqual(pv("1.2"), pv("1.2.0"))
        self.assertLess(pv("2"), pv("10"))

    def test_prerelease_before_release(self):
        self.assertLess(pv("1.0.0-alpha"), pv("1.0.0"))

    def test_prerelease_identifier_rules(self):
        chain = ["1.0.0-1", "1.0.0-a", "1.0.0-a.1", "1.0.0-a.b",
                 "1.0.0-b", "1.0.0-b.2", "1.0.0-b.11"]
        vs = [pv(s) for s in chain]
        self.assertEqual(vs, sorted(vs))
        self.assertLess(pv("1.0.0-2"), pv("1.0.0-11"))  # 数字按数值

    def test_build_metadata_tiebreak_only(self):
        # 构建元数据不影响核心/预发布优先级，仅作最终平局裁决
        self.assertLess(pv("1.0.0-zzz+aaa"), pv("1.0.0+aaa"))
        self.assertLess(pv("1.0.0+a"), pv("1.0.0+b"))
        self.assertLess(pv("1.0.0+1"), pv("1.0.0+a"))
        self.assertNotEqual(pv("1.0.0+a"), pv("1.0.0+b"))


class TestRange(unittest.TestCase):
    def test_bounds_inclusive_exclusive(self):
        r = pr(">=1.0.0 <=2.0.0")
        self.assertTrue(r.matches("1.0.0"))   # 等于下界：包含
        self.assertTrue(r.matches("2.0.0"))   # 等于上界：包含
        r2 = pr(">1.0.0 <2.0.0")
        self.assertFalse(r2.matches("1.0.0"))  # 开区间下界：排除
        self.assertFalse(r2.matches("2.0.0"))  # 开区间上界：排除

    def test_exact(self):
        r = pr("1.2.3")
        self.assertTrue(r.matches("1.2.3"))
        self.assertFalse(r.matches("1.2.4"))
        self.assertTrue(pr("=1.2.3").matches("1.2.3"))

    def test_wildcard(self):
        self.assertTrue(pr("1.2.*").matches("1.2.99"))
        self.assertFalse(pr("1.2.*").matches("1.3.0"))
        self.assertTrue(pr("1.x").matches("1.9.9"))
        self.assertFalse(pr("1.x").matches("2.0.0"))
        self.assertTrue(pr("*").matches("0.0.0"))

    def test_caret_tilde(self):
        self.assertTrue(pr("^1.2.3").matches("1.9.9"))
        self.assertFalse(pr("^1.2.3").matches("2.0.0"))
        self.assertFalse(pr("^1.2.3").matches("1.2.2"))
        self.assertTrue(pr("^0.2.3").matches("0.2.9"))
        self.assertFalse(pr("^0.2.3").matches("0.3.0"))
        self.assertTrue(pr("~1.2.3").matches("1.2.9"))
        self.assertFalse(pr("~1.2.3").matches("1.3.0"))
        self.assertTrue(pr("~1").matches("1.9.9"))

    def test_negation(self):
        r = pr("!1.2.3")
        self.assertFalse(r.matches("1.2.3"))
        self.assertTrue(r.matches("1.2.4"))

    def test_empty_interval_is_not_error(self):
        r = pr(">2.0.0 <1.0.0")
        self.assertIsInstance(r, Range)
        for v in ["0.1.0", "1.5.0", "3.0.0"]:
            self.assertFalse(r.matches(v))

    def test_prerelease_opt_in(self):
        r = pr(">=1.0.0")
        self.assertFalse(r.matches("1.2.0-rc.1"))                       # 默认排除
        self.assertTrue(r.matches("1.2.0-rc.1", include_prerelease=True))
        self.assertTrue(pr(">=1.0.0-alpha").matches(
            "1.0.0-beta", include_prerelease=True))

    def test_conjunction(self):
        r = pr(">=1.0.0 <2.0.0 !1.5.0")
        self.assertTrue(r.matches("1.4.0"))
        self.assertFalse(r.matches("1.5.0"))
        self.assertFalse(r.matches("2.0.0"))


class TestErrors(unittest.TestCase):
    def assert_error_pos(self, exc_type, text, pos):
        with self.assertRaises(exc_type) as ctx:
            pv(text) if exc_type is VersionError else pr(text)
        self.assertEqual(ctx.exception.position, pos, str(ctx.exception))

    def test_bad_versions(self):
        self.assert_error_pos(VersionError, "", 0)
        self.assert_error_pos(VersionError, "x.1.2", 0)
        self.assert_error_pos(VersionError, "01.2.3", 0)
        self.assert_error_pos(VersionError, "1.2.x", 4)
        self.assert_error_pos(VersionError, "1.2.3-", 6)
        self.assert_error_pos(VersionError, "1.2.3-a..b", 8)
        self.assert_error_pos(VersionError, "1.2.3-01", 6)   # 数字标识前导零
        self.assert_error_pos(VersionError, "1.2.3+bad_char", 9)
        self.assert_error_pos(VersionError, "1.2.3.4", 5)
        self.assert_error_pos(VersionError, "1.2.3 rc", 5)

    def test_bad_ranges(self):
        self.assert_error_pos(RangeError, ">=", 0)
        self.assert_error_pos(RangeError, "> 1.2.3", 0)
        self.assert_error_pos(RangeError, "1.2.3.4", 0)
        self.assert_error_pos(RangeError, "!1.2.*", 0)
        self.assert_error_pos(RangeError, "^1.2.3-alpha", 0)
        self.assert_error_pos(RangeError, "1.*.3", 0)
        self.assert_error_pos(RangeError, ">=1.2.*", 0)
        self.assert_error_pos(RangeError, "1.2.3+b1", 0)
        # 多 token 时定位到出错 token
        self.assert_error_pos(RangeError, ">=1.0.0 <x.y", 9)

    def test_no_string_fallback(self):
        # 非法输入必须报错，而不是退化为字符串比较
        for bad in ["v1.2.3", "1.2.3 ", " 1.2.3", "1,2,3", "latest"]:
            if bad.strip() == "1.2.3":
                continue  # 首尾空白是允许的
                # （strip 后合法属于设计行为）
            with self.assertRaises(VersionError):
                pv(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
