"""Version parsing and comparison rules (SemVer 2.0.0 section 11)."""

import unittest

from tests import *  # noqa: F401,F403  (sys.path setup)
from semverlib import Version, compare


class ParseTest(unittest.TestCase):
    def test_fields(self):
        v = Version.parse("1.2.3-alpha.1+build.42")
        self.assertEqual((v.major, v.minor, v.patch), (1, 2, 3))
        self.assertEqual(v.prerelease, ("alpha", "1"))
        self.assertEqual(v.build, ("build", "42"))
        self.assertTrue(v.is_prerelease)

    def test_roundtrip(self):
        for s in ["0.0.0", "1.2.3", "10.20.30", "1.0.0-alpha", "1.0.0-0.3.7",
                  "1.0.0-x-y-z.-", "1.0.0+build.01", "1.0.0-rc.1+build.5"]:
            self.assertEqual(str(Version.parse(s)), s)

    def test_no_leading_v_or_partial(self):
        with self.assertRaises(Exception):
            Version.parse("v1.2.3")
        with self.assertRaises(Exception):
            Version.parse("1.2")

    def test_type_error(self):
        with self.assertRaises(TypeError):
            Version.parse(123)


class CompareTest(unittest.TestCase):
    def test_semver_spec_ordering_chain(self):
        # The normative example chain from semver.org section 11.
        chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta",
                 "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
                 "1.0.0-rc.1", "1.0.0"]
        versions = [Version.parse(s) for s in chain]
        for i in range(len(versions)):
            for j in range(len(versions)):
                expected = (i > j) - (i < j)
                self.assertEqual(compare(versions[i], versions[j]), expected,
                                 "%s vs %s" % (chain[i], chain[j]))

    def test_numeric_not_string_comparison(self):
        # Guards against degenerating into lexicographic string compare.
        self.assertGreater(Version.parse("10.0.0"), Version.parse("9.0.0"))
        self.assertGreater(Version.parse("1.0.0-beta.11"),
                           Version.parse("1.0.0-beta.2"))

    def test_numeric_identifiers_below_alphanumeric(self):
        self.assertLess(Version.parse("1.0.0-1"), Version.parse("1.0.0-a"))
        self.assertLess(Version.parse("1.0.0-9"), Version.parse("1.0.0-0a"))

    def test_shorter_prerelease_list_is_lower(self):
        self.assertLess(Version.parse("1.0.0-alpha"), Version.parse("1.0.0-alpha.0"))

    def test_prerelease_before_release(self):
        self.assertLess(Version.parse("1.0.0-rc.9"), Version.parse("1.0.0"))

    def test_build_metadata_ignored_in_precedence(self):
        a = Version.parse("1.0.0+build.1")
        b = Version.parse("1.0.0+build.2")
        c = Version.parse("1.0.0")
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(compare(a, b), 0)
        self.assertFalse(a < b or b < a)
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(len({a, b, c}), 1)

    def test_total_key_is_strict(self):
        # total_key adds build metadata as a deterministic tie-breaker.
        a = Version.parse("1.0.0+a")
        b = Version.parse("1.0.0+b")
        self.assertNotEqual(a.total_key(), b.total_key())
        self.assertLess(a.total_key(), b.total_key())
        self.assertLess(Version.parse("1.0.0").total_key(), a.total_key())


if __name__ == "__main__":
    unittest.main()
