"""cliparse 自测: python3 test_cliparse.py -v"""

import contextlib
import io
import unittest

from cliparse import HelpRequested, Namespace, ParseError, Parser


def make_parser(**kwargs):
    kwargs.setdefault("exit_on_error", False)
    p = Parser(prog="tool", description="demo tool", **kwargs)
    p.add_option("--verbose", "-v", action="count", help="增加日志级别")
    p.add_option("--output", "-o", metavar="FILE", help="输出文件")
    p.add_option("--jobs", "-j", type=int, default=1, help="并发数")
    p.add_option("--tag", "-t", action="append", help="标签, 可重复")
    p.add_option("--force", "-f", action="store_true", help="强制执行")
    p.add_option("--color", action="store_false", default=True, help="关闭颜色")
    return p


def make_full_parser(**kwargs):
    """带子命令与位置参数的完整 parser。"""
    p = make_parser(**kwargs)
    p.add_positional("src", help="源文件")
    p.add_positional("dst", required=False, help="目标文件")
    run = p.add_subcommand("run", help="运行任务")
    run.add_option("--level", "-l", type=int, default=0, help="子命令级别")
    run.add_option("--dry-run", "-n", action="store_true", help="试运行")
    run.add_positional("task", help="任务名")
    p.add_option("--level", type=int, default=9, help="全局级别(与子命令同名)")
    return p


class TestBasicForms(unittest.TestCase):
    def setUp(self):
        self.p = make_parser()

    def test_long_space(self):
        ns = self.p.parse(["--output", "a.txt"])
        self.assertEqual(ns.output, "a.txt")

    def test_long_equals(self):
        ns = self.p.parse(["--output=a.txt"])
        self.assertEqual(ns.output, "a.txt")

    def test_short_space(self):
        ns = self.p.parse(["-o", "a.txt"])
        self.assertEqual(ns.output, "a.txt")

    def test_short_attached(self):
        ns = self.p.parse(["-oa.txt"])
        self.assertEqual(ns.output, "a.txt")

    def test_short_cluster_bools(self):
        ns = self.p.parse(["-fv"])
        self.assertTrue(ns.force)
        self.assertEqual(ns.verbose, 1)

    def test_short_cluster_value_at_end(self):
        ns = self.p.parse(["-fvj", "4"])
        self.assertTrue(ns.force)
        self.assertEqual(ns.verbose, 1)
        self.assertEqual(ns.jobs, 4)

    def test_short_cluster_value_attached(self):
        ns = self.p.parse(["-fj4"])
        self.assertTrue(ns.force)
        self.assertEqual(ns.jobs, 4)

    def test_bool_store_false(self):
        self.assertTrue(self.p.parse([]).color)
        self.assertFalse(self.p.parse(["--color"]).color)

    def test_count_repeat(self):
        self.assertEqual(self.p.parse(["-vvv"]).verbose, 3)
        self.assertEqual(self.p.parse(["-v", "--verbose", "-v"]).verbose, 3)

    def test_append_repeat(self):
        ns = self.p.parse(["-t", "a", "--tag=b", "-tc"])
        self.assertEqual(ns.tag, ["a", "b", "c"])

    def test_store_repeat_last_wins(self):
        ns = self.p.parse(["-o", "a", "--output", "b"])
        self.assertEqual(ns.output, "b")

    def test_defaults_applied(self):
        ns = self.p.parse([])
        self.assertEqual(ns.jobs, 1)
        self.assertEqual(ns.tag, [])
        self.assertIsNone(ns.output)
        self.assertFalse(ns.force)

    def test_defaults_not_shared_between_calls(self):
        self.p.parse(["-t", "x"])
        self.assertEqual(self.p.parse([]).tag, [])


class TestPositionalsAndInterleave(unittest.TestCase):
    def setUp(self):
        self.p = make_full_parser()

    def test_positionals(self):
        ns = self.p.parse(["in.txt", "out.txt"])
        self.assertEqual((ns.src, ns.dst), ("in.txt", "out.txt"))

    def test_optional_positional_default(self):
        ns = self.p.parse(["in.txt"])
        self.assertIsNone(ns.dst)

    def test_options_after_positionals(self):
        ns = self.p.parse(["in.txt", "--jobs", "8", "out.txt", "-f"])
        self.assertEqual((ns.src, ns.dst), ("in.txt", "out.txt"))
        self.assertEqual(ns.jobs, 8)
        self.assertTrue(ns.force)

    def test_single_dash_is_positional(self):
        ns = self.p.parse(["-"])
        self.assertEqual(ns.src, "-")

    def test_double_dash_terminator(self):
        ns = self.p.parse(["--", "--jobs", "-f"])
        self.assertEqual(ns.src, "--jobs")
        self.assertEqual(ns.dst, "-f")
        self.assertFalse(ns.force)
        self.assertEqual(ns.jobs, 1)

    def test_double_dash_extra_positionals_error(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["a", "b", "--", "c"])
        self.assertIn("unexpected extra argument", cm.exception.message)

    def test_nargs_star(self):
        p = Parser(prog="sum", exit_on_error=False)
        p.add_positional("nums", type=int, nargs="*")
        ns = p.parse(["1", "2", "3"])
        self.assertEqual(ns.nums, [1, 2, 3])
        self.assertEqual(p.parse([]).nums, [])


class TestDashValuesAndEmptyStrings(unittest.TestCase):
    def setUp(self):
        self.p = make_parser()

    def test_negative_number_value(self):
        ns = self.p.parse(["--jobs", "-5"])
        self.assertEqual(ns.jobs, -5)

    def test_dash_value_attached(self):
        ns = self.p.parse(["--jobs=-5"])
        self.assertEqual(ns.jobs, -5)
        ns = self.p.parse(["-j-5"])
        self.assertEqual(ns.jobs, -5)

    def test_registered_option_as_value_is_error(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["--output", "--force"])
        self.assertIn("requires a value", cm.exception.message)

    def test_registered_option_as_value_via_equals(self):
        ns = self.p.parse(["--output=--force"])
        self.assertEqual(ns.output, "--force")

    def test_empty_string_equals(self):
        ns = self.p.parse(["--output="])
        self.assertEqual(ns.output, "")

    def test_empty_string_separate(self):
        ns = self.p.parse(["--output", ""])
        self.assertEqual(ns.output, "")

    def test_empty_string_short_attached_impossible_but_equals_ok(self):
        ns = self.p.parse(["-o", ""])
        self.assertEqual(ns.output, "")


class TestSubcommands(unittest.TestCase):
    def setUp(self):
        self.p = make_full_parser()

    def test_only_subcommand(self):
        ns = self.p.parse(["run", "build"])
        self.assertEqual(ns.command, "run")
        self.assertEqual(ns.command_args.task, "build")
        self.assertEqual(ns.command_args.level, 0)
        self.assertFalse(ns.command_args.dry_run)

    def test_global_options_before_subcommand(self):
        ns = self.p.parse(["-v", "--jobs", "3", "run", "build"])
        self.assertEqual(ns.verbose, 1)
        self.assertEqual(ns.jobs, 3)

    def test_global_options_after_subcommand_fallback(self):
        # 子命令未定义 --jobs, 回退到全局
        ns = self.p.parse(["run", "build", "--jobs", "7"])
        self.assertEqual(ns.jobs, 7)
        self.assertNotIn("jobs", ns.command_args.as_dict())

    def test_same_name_subcommand_wins_after(self):
        # 子命令之后的 --level 解析到子命令 (就近原则)
        ns = self.p.parse(["run", "build", "--level", "2"])
        self.assertEqual(ns.level, 9)               # 全局保持默认
        self.assertEqual(ns.command_args.level, 2)  # 子命令生效

    def test_same_name_before_subcommand_is_global(self):
        ns = self.p.parse(["--level", "5", "run", "build"])
        self.assertEqual(ns.level, 5)
        self.assertEqual(ns.command_args.level, 0)

    def test_subcommand_positionals_are_separate(self):
        ns = self.p.parse(["run", "deploy", "-n"])
        self.assertEqual(ns.command_args.task, "deploy")
        self.assertTrue(ns.command_args.dry_run)
        self.assertIsNone(ns.src)  # 全局位置参数未触发? src 是必选 -> 见下

    def test_subcommand_own_error_uses_sub_usage(self):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(["run"])
        self.assertIn("missing required argument 'task'",
                      cm.exception.message)
        self.assertIn("tool run", cm.exception.usage)

    def test_unknown_subcommand_is_positional(self):
        # 未注册的词按位置参数处理
        ns = self.p.parse(["notacmd"])
        self.assertIsNone(ns.command)
        self.assertEqual(ns.src, "notacmd")


class TestErrors(unittest.TestCase):
    def setUp(self):
        self.p = make_full_parser()

    def assert_error(self, argv, *fragments):
        with self.assertRaises(ParseError) as cm:
            self.p.parse(argv)
        err = cm.exception
        self.assertEqual(err.exit_code, 2)
        text = err.format()
        self.assertIn("usage:", text)  # 错误必带用法提示
        for frag in fragments:
            self.assertIn(frag, text)
        return err

    def test_unknown_long_option(self):
        self.assert_error(["--frob"], "unrecognized option '--frob'")

    def test_unknown_short_option(self):
        self.assert_error(["-z"], "unrecognized option '-z'")

    def test_missing_value(self):
        self.assert_error(["--output"], "option '--output' requires a value")

    def test_missing_value_at_cluster_end(self):
        self.assert_error(["-fj"], "requires a value")

    def test_invalid_type(self):
        self.assert_error(["--jobs", "abc"],
                          "invalid int value: 'abc'")

    def test_invalid_choice(self):
        p = make_parser()
        p.add_option("--mode", choices=["fast", "slow"])
        with self.assertRaises(ParseError) as cm:
            p.parse(["--mode", "medium"])
        self.assertIn("invalid choice", cm.exception.message)

    def test_missing_required_positional(self):
        self.assert_error([], "missing required argument 'src'")

    def test_missing_required_option(self):
        p = make_parser()
        p.add_option("--config", required=True)
        with self.assertRaises(ParseError) as cm:
            p.parse([])
        self.assertIn("missing required option '--config'",
                      cm.exception.message)

    def test_extra_positional(self):
        self.assert_error(["a", "b", "c"], "unexpected extra argument")

    def test_flag_with_value_rejected(self):
        self.assert_error(["--force=yes"],
                          "option '--force' does not take a value")

    def test_exit_on_error_true_systemexit(self):
        p = make_full_parser(exit_on_error=True)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as cm:
                p.parse(["--nope"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("unrecognized option '--nope'", stderr.getvalue())
        self.assertIn("usage:", stderr.getvalue())

    def test_exit_on_error_false_returns_code_via_exception(self):
        # 调用方拿到错误码自行决定退出行为
        try:
            self.p.parse(["--nope"])
        except ParseError as e:
            self.assertEqual(e.exit_code, 2)
        else:
            self.fail("expected ParseError")

    def test_help_exit_zero(self):
        p = make_full_parser(exit_on_error=True)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as cm:
                p.parse(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("subcommands:", stdout.getvalue())

    def test_help_raises_when_no_exit(self):
        with self.assertRaises(HelpRequested) as cm:
            self.p.parse(["-h"])
        self.assertIn("usage: tool", cm.exception.text)


class TestEdgeCases(unittest.TestCase):
    def test_empty_argv(self):
        ns = make_parser().parse([])
        self.assertEqual(ns.verbose, 0)
        self.assertIsNone(ns.command)

    def test_empty_argv_with_subcommands_no_positionals(self):
        p = make_parser()
        p.add_subcommand("run")
        ns = p.parse([])
        self.assertIsNone(ns.command)

    def test_only_subcommand_name(self):
        p = make_parser()
        sub = p.add_subcommand("run")
        sub.add_option("--x", type=int, default=1)
        ns = p.parse(["run"])
        self.assertEqual(ns.command, "run")
        self.assertEqual(ns.command_args.x, 1)

    def test_repeated_same_option_mixed_forms(self):
        p = make_parser()
        ns = p.parse(["-t", "1", "--tag=2", "-t3", "--tag", "4"])
        self.assertEqual(ns.tag, ["1", "2", "3", "4"])

    def test_very_long_argv(self):
        p = Parser(prog="big", exit_on_error=False)
        p.add_option("--tag", "-t", action="append")
        p.add_positional("items", nargs="*")
        argv = []
        for i in range(20000):
            argv += ["-t", "tag%d" % i]
        argv += ["item%d" % i for i in range(100000)]
        ns = p.parse(argv)
        self.assertEqual(len(ns.tag), 20000)
        self.assertEqual(len(ns.items), 100000)
        self.assertEqual(ns.tag[-1], "tag19999")
        self.assertEqual(ns.items[-1], "item99999")

    def test_many_equals_and_unicode(self):
        p = make_parser()
        ns = p.parse(["--output", "中文=文件==名.txt"])
        self.assertEqual(ns.output, "中文=文件==名.txt")
        ns = p.parse(["--output=a=b=c"])
        self.assertEqual(ns.output, "a=b=c")

    def test_namespace_as_dict(self):
        ns = make_parser().parse(["-f"])
        d = ns.as_dict()
        self.assertTrue(d["force"])
        self.assertIsInstance(repr(ns), str)


if __name__ == "__main__":
    unittest.main()
