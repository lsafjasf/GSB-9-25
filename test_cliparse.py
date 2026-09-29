"""Boundary and behaviour tests for cliparse.  Run: python3 -m unittest -v"""

import unittest

from cliparse import HelpRequested, ParseError, Parser


def make_parser(**kwargs):
    kwargs.setdefault("exit_on_error", False)
    p = Parser(prog="tool", **kwargs)
    p.add_option("--verbose", "-v", help="be verbose")
    p.add_option("--output", "-o", takes_value=True, metavar="FILE",
                 default="out.txt")
    p.add_option("--include", "-I", takes_value=True, repeat=True,
                 metavar="DIR")
    p.add_option("--count", "-c", takes_value=True, type=int, default=0)
    return p


class TestLongAndShortOptions(unittest.TestCase):
    def setUp(self):
        self.p = make_parser()

    def test_long_space_value(self):
        r = self.p.parse(["--output", "a.txt"])
        self.assertEqual(r.output, "a.txt")

    def test_long_equals_value(self):
        r = self.p.parse(["--output=a.txt"])
        self.assertEqual(r.output, "a.txt")

    def test_short_space_value(self):
        r = self.p.parse(["-o", "a.txt"])
        self.assertEqual(r.output, "a.txt")

    def test_short_attached_value(self):
        r = self.p.parse(["-oa.txt"])
        self.assertEqual(r.output, "a.txt")

    def test_short_equals_value(self):
        r = self.p.parse(["-o=a.txt"])
        self.assertEqual(r.output, "a.txt")

    def test_short_equals_empty_value(self):
        # -o= attaches an explicit empty string; it must not swallow the
        # following token as its value, and must not be treated as missing.
        self.p.add_positional("src")
        r = self.p.parse(["-o=", "pos"])
        self.assertEqual(r.output, "")
        self.assertEqual(r.positionals, ["pos"])
        self.assertEqual(len(r.positionals), 1)
        bare = make_parser()
        self.assertEqual(bare.parse(["-o="]).output, "")
        with self.assertRaises(ParseError):
            bare.parse(["-o"])

    def test_short_equals_value_with_positional(self):
        # -o=a.txt takes only the inline value; the '=' is not dropped in a
        # way that shifts positional arguments.
        self.p.add_positional("src")
        r = self.p.parse(["-o=a.txt", "pos"])
        self.assertEqual(r.output, "a.txt")
        self.assertEqual(r.positionals, ["pos"])
        self.assertEqual(len(r.positionals), 1)

    def test_short_space_empty_value_with_positional(self):
        # -o "" is a real empty-string value supplied as its own token.
        self.p.add_positional("src")
        r = self.p.parse(["-o", "", "pos"])
        self.assertEqual(r.output, "")
        self.assertEqual(r.positionals, ["pos"])
        self.assertEqual(len(r.positionals), 1)

    def test_boolean_flag(self):
        r = self.p.parse(["--verbose"])
        self.assertIs(r.verbose, True)
        r = self.p.parse([])
        self.assertIs(r.verbose, False)

    def test_short_cluster_of_flags(self):
        self.p.add_option("--force", "-f")
        self.p.add_option("--quiet", "-q")
        r = self.p.parse(["-vfq"])
        self.assertTrue(r.verbose and r.force and r.quiet)

    def test_short_cluster_with_value_at_end(self):
        self.p.add_option("--force", "-f")
        r = self.p.parse(["-vfo", "a.txt"])
        self.assertTrue(r.verbose and r.force)
        self.assertEqual(r.output, "a.txt")

    def test_short_cluster_with_attached_value(self):
        self.p.add_option("--force", "-f")
        r = self.p.parse(["-vfoa.txt"])
        self.assertEqual(r.output, "a.txt")

    def test_value_option_mid_cluster_takes_rest_as_value(self):
        # -ovalue: 'o' consumes the remainder of the cluster.
        r = self.p.parse(["-ov"])
        self.assertEqual(r.output, "v")
        self.assertIs(r.verbose, False)


class TestRepeatAndDefaults(unittest.TestCase):
    def setUp(self):
        self.p = make_parser()

    def test_repeat_collects_in_order(self):
        r = self.p.parse(["-I", "a", "--include=b", "-Ic"])
        self.assertEqual(r.include, ["a", "b", "c"])

    def test_repeat_default_is_empty_list(self):
        r = self.p.parse([])
        self.assertEqual(r.include, [])

    def test_repeat_list_not_shared_between_parses(self):
        r1 = self.p.parse(["-I", "a"])
        r2 = self.p.parse([])
        self.assertEqual(r1.include, ["a"])
        self.assertEqual(r2.include, [])

    def test_repeated_single_option_last_wins(self):
        r = self.p.parse(["-o", "a", "--output", "b", "--output=c"])
        self.assertEqual(r.output, "c")

    def test_repeated_flag_stays_true(self):
        r = self.p.parse(["-v", "-v", "--verbose"])
        self.assertIs(r.verbose, True)

    def test_repeat_flag_counts(self):
        p = Parser(prog="t", exit_on_error=False)
        p.add_option("--verbose", "-v", repeat=True)
        r = p.parse(["-vvv", "--verbose"])
        self.assertEqual(r.verbose, 4)

    def test_defaults_applied(self):
        r = self.p.parse([])
        self.assertEqual(r.output, "out.txt")
        self.assertEqual(r.count, 0)


class TestPositionals(unittest.TestCase):
    def setUp(self):
        self.p = make_parser()
        self.p.add_positional("src")
        self.p.add_positional("dst", required=False)

    def test_positionals_collected(self):
        r = self.p.parse(["in.txt", "out.txt"])
        self.assertEqual(r.positionals, ["in.txt", "out.txt"])

    def test_options_after_positionals(self):
        r = self.p.parse(["in.txt", "--verbose", "-o", "x", "out.txt"])
        self.assertEqual(r.positionals, ["in.txt", "out.txt"])
        self.assertIs(r.verbose, True)
        self.assertEqual(r.output, "x")

    def test_missing_required_positional(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["--verbose"])
        self.assertIn("missing required positional argument: src",
                      cm.exception.message)
        self.assertIn("usage:", cm.exception.usage)

    def test_extra_positionals_kept(self):
        r = self.p.parse(["a", "b", "c", "d"])
        self.assertEqual(r.positionals, ["a", "b", "c", "d"])


class TestDashValuesAndTerminator(unittest.TestCase):
    def setUp(self):
        self.p = make_parser()

    def test_negative_number_value(self):
        r = self.p.parse(["--count", "-5"])
        self.assertEqual(r.count, -5)

    def test_negative_number_equals(self):
        r = self.p.parse(["--count=-5"])
        self.assertEqual(r.count, -5)

    def test_dash_value_via_equals(self):
        r = self.p.parse(["--output=-weird"])
        self.assertEqual(r.output, "-weird")

    def test_dash_value_space_separated_is_missing_value(self):
        # A token that looks like an option is not swallowed as a value.
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["--output", "--verbose"])
        self.assertIn("requires a value", cm.exception.message)

    def test_bare_dash_is_a_value(self):
        r = self.p.parse(["--output", "-"])
        self.assertEqual(r.output, "-")

    def test_empty_value_via_equals(self):
        r = self.p.parse(["--output="])
        self.assertEqual(r.output, "")

    def test_empty_value_via_space(self):
        r = self.p.parse(["--output", ""])
        self.assertEqual(r.output, "")

    def test_double_dash_terminator(self):
        r = self.p.parse(["--verbose", "--", "--output", "-x", "pos"])
        self.assertIs(r.verbose, True)
        self.assertEqual(r.output, "out.txt")  # untouched default
        self.assertEqual(r.positionals, ["--output", "-x", "pos"])

    def test_terminator_first(self):
        r = self.p.parse(["--", "--verbose"])
        self.assertIs(r.verbose, False)
        self.assertEqual(r.positionals, ["--verbose"])

    def test_negative_number_as_positional(self):
        r = self.p.parse(["-1", "-3.14"])
        self.assertEqual(r.positionals, ["-1", "-3.14"])


class TestSubcommands(unittest.TestCase):
    def setUp(self):
        self.p = make_parser()
        self.run = self.p.add_subcommand("run", help="run the thing")
        self.run.add_option("--verbose", "-v", takes_value=True,
                            metavar="LEVEL")  # shadows global -v
        self.run.add_option("--jobs", "-j", takes_value=True, type=int,
                            default=1)
        self.run.add_positional("target")

    def test_global_before_subcommand(self):
        r = self.p.parse(["-v", "run", "app"])
        self.assertIs(r.verbose, True)          # global flag
        self.assertEqual(r.subcommand, "run")
        self.assertIsNone(r.sub.verbose)        # sub option unset
        self.assertEqual(r.sub.positionals, ["app"])

    def test_subcommand_option_shadows_global_after_token(self):
        r = self.p.parse(["run", "-v", "3", "app"])
        self.assertIs(r.verbose, False)         # global untouched
        self.assertEqual(r.sub.verbose, "3")    # subcommand's -v wins here

    def test_same_name_both_sides(self):
        r = self.p.parse(["-v", "run", "-v", "3", "app"])
        self.assertIs(r.verbose, True)
        self.assertEqual(r.sub.verbose, "3")

    def test_subcommand_defaults(self):
        r = self.p.parse(["run", "app"])
        self.assertEqual(r.sub.jobs, 1)

    def test_subcommand_only_no_args(self):
        p = Parser(prog="t", exit_on_error=False)
        p.add_subcommand("init")
        r = p.parse(["init"])
        self.assertEqual(r.subcommand, "init")
        self.assertEqual(r.sub.positionals, [])

    def test_subcommand_missing_its_positional(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["run"])
        self.assertIn("target", cm.exception.message)
        self.assertIn("tool run", cm.exception.usage)

    def test_subcommand_unknown_option_reported_by_subparser(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["run", "--nope", "app"])
        self.assertIn("unknown option: --nope", cm.exception.message)
        self.assertIn("tool run", cm.exception.usage)

    def test_subcommand_required(self):
        p = Parser(prog="t", exit_on_error=False, subcommand_required=True)
        p.add_subcommand("go")
        with self.assertRaises(ParseError) as cm:
            p.parse([])
        self.assertIn("missing subcommand", cm.exception.message)

    def test_no_subcommand_given_is_ok_when_not_required(self):
        r = self.p.parse([])
        self.assertIsNone(r.subcommand)
        self.assertIsNone(r.sub)


class TestErrors(unittest.TestCase):
    def setUp(self):
        self.p = make_parser()

    def test_unknown_long_option(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["--verboes"])
        self.assertIn("unknown option: --verboes", cm.exception.message)
        self.assertIn("did you mean --verbose?", cm.exception.message)
        self.assertEqual(cm.exception.exit_code, 2)

    def test_unknown_short_option(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["-z"])
        self.assertIn("unknown option: -z", cm.exception.message)

    def test_missing_value_at_end(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["--output"])
        self.assertIn("option --output, -o FILE requires a value",
                      cm.exception.message)

    def test_invalid_type(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["--count", "abc"])
        self.assertIn("invalid int value: 'abc'", cm.exception.message)

    def test_flag_rejects_value(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["--verbose=yes"])
        self.assertIn("does not take a value", cm.exception.message)

    def test_error_format_includes_usage(self):
        try:
            self.p.parse(["--nope"])
        except ParseError as e:
            text = e.format()
        self.assertTrue(text.startswith("usage: tool"))
        self.assertIn("error: unknown option: --nope", text)

    def test_exit_on_error_true_raises_system_exit(self):
        p = make_parser()
        p.exit_on_error = True
        with self.assertRaises(SystemExit) as cm:
            p.parse(["--nope"])
        self.assertEqual(cm.exception.code, 2)

    def test_exit_on_error_false_never_exits(self):
        for argv in (["--nope"], ["--output"], ["--count", "x"]):
            with self.assertRaises(ParseError):
                self.p.parse(argv)

    def test_help_requested(self):
        with self.assertRaises(HelpRequested) as cm:
            self.p.parse(["--help"])
        self.assertEqual(cm.exception.exit_code, 0)
        self.assertIn("usage: tool", cm.exception.text)
        self.assertIn("--output, -o FILE", cm.exception.text)

    def test_help_exits_zero_when_exit_on_error(self):
        p = make_parser()
        p.exit_on_error = True
        with self.assertRaises(SystemExit) as cm:
            p.parse(["-h"])
        self.assertEqual(cm.exception.code, 0)


class TestEdgeCases(unittest.TestCase):
    def test_empty_argv(self):
        r = make_parser().parse([])
        self.assertEqual(r.positionals, [])
        self.assertIs(r.verbose, False)
        self.assertIsNone(r.subcommand)

    def test_only_terminator(self):
        r = make_parser().parse(["--"])
        self.assertEqual(r.positionals, [])

    def test_empty_string_positional(self):
        r = make_parser().parse([""])
        self.assertEqual(r.positionals, [""])

    def test_repeated_same_option_mixed_forms(self):
        p = make_parser()
        r = p.parse(["-o", "1", "--output=2", "-o3", "--output", "4"])
        self.assertEqual(r.output, "4")

    def test_very_long_argument_list(self):
        p = make_parser()
        p.add_positional("src")
        argv = []
        for i in range(5000):
            argv += ["--include", "dir%d" % i, "file%d" % i]
        argv += ["-v"] * 1000
        r = p.parse(argv)
        self.assertEqual(len(r.include), 5000)
        self.assertEqual(len(r.positionals), 5000)
        self.assertIs(r.verbose, True)

    def test_value_that_is_exactly_double_dash_is_missing(self):
        p = make_parser()
        with self.assertRaises(ParseError):
            p.parse(["--output", "--"])

    def test_unicode_and_spaces_in_values(self):
        p = make_parser()
        r = p.parse(["--output", "中文 文件.txt"])
        self.assertEqual(r.output, "中文 文件.txt")


if __name__ == "__main__":
    unittest.main()
