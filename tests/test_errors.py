"""Invalid versions and ranges must raise positioned errors."""

import unittest

from tests import *  # noqa: F401,F403  (sys.path setup)
from semverlib import Range, Version
from semverlib.errors import RangeParseError, VersionParseError


class VersionErrorTest(unittest.TestCase):
    CASES = [
        ("", 0, "expected major version number"),
        ("1", 1, "expected '.' after major version"),
        ("1.2", 3, "expected '.' after minor version"),
        ("01.2.3", 0, "leading zero in major version"),
        ("1.02.3", 2, "leading zero in minor version"),
        ("1.2.03", 4, "leading zero in patch version"),
        ("1.2.3-", 6, "empty prerelease identifier"),
        ("1.2.3-a..b", 8, "empty prerelease identifier"),
        ("1.2.3-alpha..1", 12, "empty prerelease identifier"),
        ("1.2.3-01", 6, "leading zero in numeric prerelease identifier"),
        ("1.2.3+", 6, "empty build identifier"),
        ("1.2.3-a_b", 7, "invalid character '_' in prerelease"),
        ("1.2.3-alpha_1", 11, "invalid character '_' in prerelease"),
        ("1.2.3.4", 5, "unexpected character '.'"),
        ("v1.2.3", 0, "expected major version number"),
        ("1.2.3-a+b+c", 9, "invalid character '+' in build"),
        ("1.2.x", 4, "expected patch version number"),
        ("-1.2.3", 0, "expected major version number"),
        ("1.2.3 ", 5, "unexpected character ' '"),
    ]

    def test_positions_and_messages(self):
        for source, position, message in self.CASES:
            with self.subTest(source=source):
                with self.assertRaises(VersionParseError) as ctx:
                    Version.parse(source)
                err = ctx.exception
                self.assertEqual(err.position, position)
                self.assertEqual(err.message, message)
                self.assertEqual(err.source, source)

    def test_pointer_rendering(self):
        try:
            Version.parse("1.02.3")
        except VersionParseError as err:
            self.assertEqual(err.pointer, "1.02.3\n  ^")
        else:
            self.fail("expected VersionParseError")


class RangeErrorTest(unittest.TestCase):
    CASES = [
        ("|", 0, "expected '||'"),
        ("1.2.3 |", 6, "expected '||'"),
        ("|| 1.2.3", 0, "empty comparator set"),
        ("1.2.3 ||", 6, "empty comparator set"),
        ("1.2.3 || || 2.0.0", 9, "empty comparator set"),
        (">=", 2, "missing version after '>='"),
        ("~", 1, "missing version after '~'"),
        ("=", 1, "missing version after '='"),
        ("1.02.3", 2, "leading zero in version number"),
        ("1.2.3.4", 6, "expected at most three version parts"),
        ("1.x.2", 4, "number cannot follow a wildcard"),
        ("1.2.x-beta", 5,
         "wildcard or partial version cannot carry prerelease/build metadata"),
        ("1.2.3-", 6, "empty prerelease identifier"),
        ("1.2.3-01", 6, "leading zero in numeric prerelease identifier"),
        ("foo", 0, "invalid version part 'foo'"),
        (">=>1.2", 2, "invalid version part '>1'"),
        ("!<1.2.3", 1, "invalid version part '<1'"),
        ("1.2.3+a..b", 8, "empty build identifier"),
        ("1..2", 2, "empty version part"),
    ]

    def test_positions_and_messages(self):
        for source, position, message in self.CASES:
            with self.subTest(source=source):
                with self.assertRaises(RangeParseError) as ctx:
                    Range.parse(source)
                err = ctx.exception
                self.assertEqual(err.position, position)
                self.assertEqual(err.message, message)

    def test_type_error(self):
        with self.assertRaises(TypeError):
            Range.parse(None)


if __name__ == "__main__":
    unittest.main()
