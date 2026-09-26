"""Boundary case list for range matching.

Each test documents one rule; the same list is mirrored in README.md.
"""

import unittest

from tests import *  # noqa: F401,F403  (sys.path setup)
from semverlib import Range, Version, match


class ExactMatchTest(unittest.TestCase):
    def test_bare_and_equals(self):
        self.assertTrue(match("1.2.3", "1.2.3"))
        self.assertTrue(match("=1.2.3", "1.2.3"))
        self.assertTrue(match("==1.2.3", "1.2.3"))
        self.assertFalse(match("1.2.3", "1.2.4"))
        self.assertFalse(match("1.2.3", "1.2.2"))

    def test_build_metadata_ignored(self):
        self.assertTrue(match("=1.2.3+foo", "1.2.3+bar"))
        self.assertTrue(match("=1.2.3", "1.2.3+anything"))
        self.assertTrue(match("1.2.3+sha.1", "1.2.3"))

    def test_exact_prerelease(self):
        self.assertTrue(match("=1.0.0-alpha", "1.0.0-alpha",
                              include_prerelease=True))
        self.assertFalse(match("=1.0.0-alpha", "1.0.0-alpha"))  # default off
        self.assertFalse(match("=1.0.0-alpha", "1.0.0-alpha.1",
                               include_prerelease=True))


class BoundTest(unittest.TestCase):
    def test_lower_bound_inclusive(self):
        # 等于下界 -> 包含
        self.assertTrue(match(">=1.2.3", "1.2.3"))
        self.assertFalse(match(">1.2.3", "1.2.3"))
        self.assertTrue(match(">1.2.3", "1.2.4"))

    def test_upper_bound(self):
        # 等于上界: <= 包含, < 排除
        self.assertTrue(match("<=2.0.0", "2.0.0"))
        self.assertFalse(match("<2.0.0", "2.0.0"))
        self.assertTrue(match("<2.0.0", "1.9.9"))

    def test_closed_interval_single_point(self):
        r = ">=2.0.0 <=2.0.0"
        self.assertTrue(match(r, "2.0.0"))
        self.assertFalse(match(r, "2.0.1"))
        self.assertFalse(match(r, "1.9.9"))

    def test_empty_interval_matches_nothing(self):
        # 区间为空 -> 什么都不匹配
        for r in (">2.0.0 <2.0.0", ">=2.0.0 <2.0.0", ">2.0.0 <=2.0.0",
                  ">=3.0.0 <2.0.0", "<1.0.0 >1.0.0"):
            for v in ("1.0.0", "2.0.0", "2.0.1", "3.0.0"):
                self.assertFalse(match(r, v), "%r vs %r" % (r, v))

    def test_combined_set(self):
        r = ">=1.2.3 <2.0.0"
        self.assertTrue(match(r, "1.2.3"))
        self.assertTrue(match(r, "1.9.9"))
        self.assertFalse(match(r, "1.2.2"))
        self.assertFalse(match(r, "2.0.0"))


class WildcardTest(unittest.TestCase):
    def test_patch_wildcard(self):
        self.assertTrue(match("1.2.x", "1.2.0"))
        self.assertTrue(match("1.2.*", "1.2.99"))
        self.assertTrue(match("1.2", "1.2.5"))
        self.assertFalse(match("1.2.x", "1.3.0"))
        self.assertFalse(match("1.2.x", "1.1.9"))

    def test_minor_wildcard(self):
        self.assertTrue(match("1.x", "1.0.0"))
        self.assertTrue(match("1.X", "1.99.0"))
        self.assertFalse(match("1.x", "2.0.0"))

    def test_any(self):
        for r in ("*", "x", ""):
            self.assertTrue(match(r, "0.0.0"))
            self.assertTrue(match(r, "99.99.99+build"))

    def test_wildcard_with_operators(self):
        self.assertTrue(match(">1.2.x", "1.3.0"))
        self.assertFalse(match(">1.2.x", "1.2.99"))
        self.assertTrue(match("<1.2.x", "1.1.9"))
        self.assertFalse(match("<1.2.x", "1.2.0"))
        self.assertTrue(match("<=1.2.x", "1.2.9"))
        self.assertFalse(match("<=1.2.x", "1.3.0"))
        self.assertTrue(match(">=1.2.x", "1.2.0"))

    def test_degenerate_operators_on_any(self):
        # >/</!= of "any" is unsatisfiable; everything else matches all.
        self.assertFalse(match(">*", "1.0.0"))
        self.assertFalse(match("<*", "1.0.0"))
        self.assertFalse(match("!=*", "1.0.0"))
        self.assertTrue(match(">=*", "1.0.0"))
        self.assertTrue(match("~*", "1.0.0"))
        self.assertTrue(match("^x", "1.0.0"))


class CompatibleTest(unittest.TestCase):
    """~ (patch-level) and ^ (major/minor-level) compatibility."""

    def test_tilde(self):
        self.assertTrue(match("~1.2.3", "1.2.9"))
        self.assertFalse(match("~1.2.3", "1.3.0"))
        self.assertFalse(match("~1.2.3", "1.2.2"))
        self.assertTrue(match("~1.2", "1.2.0"))
        self.assertFalse(match("~1.2", "1.3.0"))
        self.assertTrue(match("~1", "1.5.0"))
        self.assertFalse(match("~1", "2.0.0"))
        self.assertTrue(match("~0.2.3", "0.2.9"))
        self.assertFalse(match("~0.2.3", "0.3.0"))

    def test_caret(self):
        self.assertTrue(match("^1.2.3", "1.9.0"))
        self.assertFalse(match("^1.2.3", "2.0.0"))
        self.assertFalse(match("^1.2.3", "1.2.2"))
        # 0.x special rules: leftmost non-zero component is the "breaking" one
        self.assertTrue(match("^0.2.3", "0.2.9"))
        self.assertFalse(match("^0.2.3", "0.3.0"))
        self.assertTrue(match("^0.0.3", "0.0.3"))
        self.assertFalse(match("^0.0.3", "0.0.4"))
        self.assertTrue(match("^0.0", "0.0.9"))
        self.assertFalse(match("^0.0", "0.1.0"))
        self.assertTrue(match("^0", "0.9.9"))
        self.assertFalse(match("^0", "1.0.0"))

    def test_tilde_caret_with_prerelease_lower_bound(self):
        r = "^1.2.3-beta"
        self.assertTrue(match(r, "1.2.3-beta", include_prerelease=True))
        self.assertTrue(match(r, "1.2.3", include_prerelease=True))
        self.assertFalse(match(r, "1.2.3-alpha", include_prerelease=True))
        self.assertFalse(match(r, "2.0.0", include_prerelease=True))


class NegationTest(unittest.TestCase):
    def test_not_equal_exact(self):
        self.assertFalse(match("!=1.2.3", "1.2.3"))
        self.assertFalse(match("!1.2.3", "1.2.3"))
        self.assertTrue(match("!=1.2.3", "1.2.2"))
        self.assertTrue(match("!=1.2.3", "1.2.4"))

    def test_not_equal_wildcard(self):
        self.assertFalse(match("!=1.2.x", "1.2.5"))
        self.assertTrue(match("!=1.2.x", "1.1.9"))
        self.assertTrue(match("!=1.2.x", "1.3.0"))

    def test_negation_inside_set(self):
        r = ">=1.0.0 !=1.2.3 <2.0.0"
        self.assertTrue(match(r, "1.2.2"))
        self.assertFalse(match(r, "1.2.3"))
        self.assertFalse(match(r, "2.0.0"))


class OrTest(unittest.TestCase):
    def test_alternatives(self):
        r = "<1.0.0 || >=2.0.0"
        self.assertTrue(match(r, "0.9.9"))
        self.assertTrue(match(r, "2.0.0"))
        self.assertFalse(match(r, "1.5.0"))

    def test_max_satisfying_across_sets(self):
        versions = [Version.parse(s) for s in
                    ("0.9.0", "1.0.0", "1.5.0", "2.0.0", "2.1.0")]
        best = Range.parse("^1.0.0 || >=2.0.0").max_satisfying(versions)
        self.assertEqual(best, Version.parse("2.1.0"))


class PrereleaseGateTest(unittest.TestCase):
    def test_prerelease_versions_excluded_by_default(self):
        r = ">=1.0.0-alpha <1.0.0"
        self.assertFalse(match(r, "1.0.0-beta"))
        self.assertTrue(match(r, "1.0.0-beta", include_prerelease=True))
        # release versions are unaffected by the gate
        self.assertFalse(match(">=1.0.0-alpha <1.0.0", "1.0.0"))
        self.assertTrue(match(">=1.0.0-alpha <=1.0.0", "1.0.0"))

    def test_star_excludes_prerelease_by_default(self):
        self.assertFalse(match("*", "1.0.0-alpha"))
        self.assertTrue(match("*", "1.0.0-alpha", include_prerelease=True))

    def test_filter_and_max_satisfying(self):
        versions = [Version.parse(s) for s in
                    ("1.0.0-alpha", "1.0.0", "1.1.0-rc.1", "1.1.0", "2.0.0")]
        r = Range.parse(">=1.0.0 <2.0.0")
        self.assertEqual([str(v) for v in r.filter(versions)],
                         ["1.0.0", "1.1.0"])
        self.assertEqual(str(r.max_satisfying(versions)), "1.1.0")
        self.assertEqual(
            str(r.max_satisfying(versions, include_prerelease=True)), "1.1.0")


if __name__ == "__main__":
    unittest.main()
