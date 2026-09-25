"""assertlib 自测：覆盖各类失败场景与消息稳定性。"""
import time
import unittest

from assertlib import (
    AssertionFailure,
    assert_almost_equal,
    assert_completes_within,
    assert_contains,
    assert_equal,
    assert_not_contains,
    assert_raises,
)


def fail_message(fn, *args, **kwargs):
    """执行断言并返回失败消息文本。"""
    try:
        fn(*args, **kwargs)
    except AssertionFailure as exc:
        return str(exc)
    raise AssertionError(f"{fn.__name__} did not fail")


class TestEqualPass(unittest.TestCase):
    def test_scalars(self):
        assert_equal(1, 1)
        assert_equal("a", "a")
        assert_equal(None, None)
        assert_equal(1, 1.0)  # int/float 数值相等

    def test_nested(self):
        assert_equal({"a": [1, {"b": (2, 3)}]}, {"a": [1, {"b": (2, 3)}]})

    def test_sets(self):
        assert_equal({1, 2, 3}, {3, 2, 1})


class TestEqualDiffPaths(unittest.TestCase):
    def test_nested_dict_path(self):
        actual = {"user": {"name": "ann", "tags": ["x", "y"]}}
        expected = {"user": {"name": "bob", "tags": ["x", "y"]}}
        msg = fail_message(assert_equal, actual, expected)
        self.assertIn("root.user.name", msg)
        self.assertIn("expected: 'bob'", msg)
        self.assertIn("actual:   'ann'", msg)

    def test_list_index_path(self):
        msg = fail_message(assert_equal, [1, 2, 99], [1, 2, 3])
        self.assertIn("root[2]", msg)
        self.assertIn("expected: 3", msg)
        self.assertIn("actual:   99", msg)

    def test_deep_mixed_path(self):
        actual = {"items": [{"id": 1}, {"id": 2, "v": "a"}]}
        expected = {"items": [{"id": 1}, {"id": 2, "v": "b"}]}
        msg = fail_message(assert_equal, actual, expected)
        self.assertIn("root.items[1].v", msg)

    def test_missing_and_unexpected_keys(self):
        msg = fail_message(assert_equal, {"a": 1, "c": 3}, {"a": 1, "b": 2})
        self.assertIn("missing key 'b'", msg)
        self.assertIn("unexpected key 'c'", msg)

    def test_type_mismatch(self):
        msg = fail_message(assert_equal, {"n": "3"}, {"n": 3})
        self.assertIn("type mismatch: expected int, got str", msg)
        self.assertIn("root.n", msg)

    def test_bool_vs_int_is_type_mismatch(self):
        msg = fail_message(assert_equal, True, 1)
        self.assertIn("type mismatch: expected int, got bool", msg)

    def test_set_diff(self):
        msg = fail_message(assert_equal, {1, 2, 4}, {1, 2, 3})
        self.assertIn("missing:   [3]", msg)
        self.assertIn("unexpected: [4]", msg)

    def test_max_diffs_cap(self):
        actual = {f"k{i}": i for i in range(50)}
        expected = {f"k{i}": -i for i in range(50)}
        msg = fail_message(assert_equal, actual, expected, max_diffs=5)
        self.assertIn("further differences suppressed after 5", msg)


class TestSequenceAlignment(unittest.TestCase):
    """插入 / 删除识别的多组对照样例。"""

    def test_insert_middle(self):
        actual = ["a", "b", "c", "X", "d", "e"]
        expected = ["a", "b", "c", "d", "e"]
        msg = fail_message(assert_equal, actual, expected)
        self.assertIn("inserted 1 item(s) at index 3", msg)
        self.assertIn("inserted: ['X']", msg)
        self.assertNotIn("deleted", msg)

    def test_insert_head(self):
        msg = fail_message(assert_equal, ["X", "a", "b"], ["a", "b"])
        self.assertIn("inserted 1 item(s) at index 0", msg)

    def test_insert_tail(self):
        msg = fail_message(assert_equal, ["a", "b", "X", "Y"], ["a", "b"])
        self.assertIn("inserted 2 item(s) at index 2", msg)

    def test_delete_middle(self):
        msg = fail_message(assert_equal, ["a", "b", "e"], ["a", "b", "c", "d", "e"])
        self.assertIn("deleted 2 item(s) at index 2", msg)
        self.assertIn("deleted: ['c', 'd']", msg)

    def test_insert_and_delete_together(self):
        actual = [1, 2, 9, 4]
        expected = [1, 3, 4, 5]
        msg = fail_message(assert_equal, actual, expected)
        self.assertIn("inserted", msg)
        self.assertIn("deleted", msg)

    def test_insert_of_unhashable_items(self):
        actual = [{"k": 1}, {"k": 2}, {"k": 3}]
        expected = [{"k": 1}, {"k": 3}]
        msg = fail_message(assert_equal, actual, expected)
        self.assertIn("inserted 1 item(s) at index 1", msg)

    def test_replace_recurses_into_elements(self):
        actual = [{"v": "new"}]
        expected = [{"v": "old"}]
        msg = fail_message(assert_equal, actual, expected)
        self.assertIn("root[0].v", msg)

    def test_tuple_insert(self):
        msg = fail_message(assert_equal, (1, 2, 3, 4), (1, 4))
        self.assertIn("tuple: inserted 2 item(s) at index 1", msg)
        self.assertIn("inserted: [2, 3]", msg)


class TestTruncationKeepsLocation(unittest.TestCase):
    def test_long_string_offset_preserved(self):
        expected = "lorem-" * 100 + "MIDDLE" + "ipsum-" * 100
        actual = expected.replace("MIDDLE", "M1DDLE")
        msg = fail_message(assert_equal, actual, expected)
        # 偏移 = 6*100 + 1 = 601（'I' 被改成 '1'），必须出现在消息里
        self.assertIn("string differs at offset 601", msg)
        self.assertIn("expected len 1206, actual len 1206", msg)
        # 上下文窗口带绝对区间，且被截断标记包围
        self.assertIn("expected[581:621]:", msg)
        self.assertIn("...", msg)
        # 完整文本不得出现（1206 字符）
        self.assertLess(len(msg), 600)

    def test_long_string_length_change(self):
        expected = "a" * 500
        actual = "a" * 250 + "b" + "a" * 250
        msg = fail_message(assert_equal, actual, expected)
        self.assertIn("offset 250", msg)
        self.assertIn("expected len 500, actual len 501", msg)

    def test_long_list_path_preserved(self):
        expected = list(range(1000))
        actual = list(range(1000))
        actual[777] = -1
        msg = fail_message(assert_equal, actual, expected)
        self.assertIn("root[777]", msg)
        self.assertIn("expected: 777", msg)
        self.assertIn("actual:   -1", msg)
        self.assertLess(len(msg), 400)

    def test_long_value_repr_truncated_with_length(self):
        big = ["x" * 200] * 10  # repr 很长但不是字符串，走 _fmt 截断
        msg = fail_message(assert_equal, {"k": big}, {"k": ["y"]})
        self.assertIn("truncated", msg)
        self.assertIn("chars total", msg)
        self.assertIn("root.k", msg)
        self.assertLess(len(msg), 500)

    def test_deep_path_in_long_list(self):
        expected = [{"id": i, "payload": "p" * 300} for i in range(50)]
        actual = [dict(d) for d in expected]
        actual[42]["payload"] = "q" * 300
        msg = fail_message(assert_equal, actual, expected)
        self.assertIn("root[42].payload", msg)
        self.assertIn("offset 0", msg)


class TestAlmostEqual(unittest.TestCase):
    def test_pass_within_tol(self):
        assert_almost_equal(1.0000001, 1.0, tol=1e-5)

    def test_pass_within_rel(self):
        assert_almost_equal(101.0, 100.0, rel=0.02)

    def test_fail_reports_diff_tol_relerr(self):
        msg = fail_message(assert_almost_equal, 3.14160, 3.14159, tol=1e-6)
        self.assertIn("abs diff:  1.000000000", msg[:200].replace("\n", " ") + msg)
        self.assertIn("tolerance: 1e-06 (abs)", msg)
        self.assertIn("rel error:", msg)
        self.assertIn("expected:  3.14159", msg)
        self.assertIn("actual:    3.1416", msg)

    def test_rel_tolerance_reported(self):
        msg = fail_message(assert_almost_equal, 110.0, 100.0, tol=1e-9, rel=0.05)
        self.assertIn("tolerance: 1e-09 (abs), 0.05 (rel)", msg)
        self.assertIn("rel error: 0.1", msg)

    def test_type_mismatch(self):
        msg = fail_message(assert_almost_equal, "3.14", 3.14)
        self.assertIn("type mismatch", msg)
        self.assertIn("str", msg)

    def test_zero_expected_rel_error_inf(self):
        msg = fail_message(assert_almost_equal, 0.5, 0.0, tol=1e-9)
        self.assertIn("rel error: inf", msg)


class TestRaises(unittest.TestCase):
    def test_pass_callable(self):
        exc = assert_raises(ValueError, int, "abc")
        self.assertIsInstance(exc, ValueError)

    def test_pass_context_manager(self):
        with assert_raises(KeyError):
            {}["nope"]

    def test_fail_nothing_raised(self):
        msg = fail_message(assert_raises, ValueError, lambda: None)
        self.assertIn("expected ValueError, but nothing was raised", msg)

    def test_fail_wrong_type(self):
        def boom():
            raise KeyError("k")
        msg = fail_message(assert_raises, ValueError, boom)
        self.assertIn("expected ValueError, but KeyError was raised", msg)

    def test_context_manager_fail(self):
        with self.assertRaises(AssertionFailure):
            with assert_raises(ValueError):
                pass


class TestTimeout(unittest.TestCase):
    def test_fast_function_passes(self):
        result = assert_completes_within(2.0, lambda: 40 + 2)
        self.assertEqual(result, 42)

    def test_slow_function_fails(self):
        msg = fail_message(assert_completes_within, 0.05, time.sleep, 5)
        self.assertIn("did not complete within 0.050s", msg)

    def test_exception_propagates(self):
        def boom():
            raise RuntimeError("inner")
        with self.assertRaises(RuntimeError):
            assert_completes_within(1.0, boom)


class TestContains(unittest.TestCase):
    def test_pass(self):
        assert_contains([1, 2, 3], 2)
        assert_contains({"k": 1}, "k")
        assert_contains("hello world", "world")
        assert_not_contains([1, 2], 9)

    def test_fail_list(self):
        msg = fail_message(assert_contains, [1, 2, 3], 9)
        self.assertIn("item not found: 9", msg)
        self.assertIn("container (list, len 3)", msg)

    def test_fail_long_string_keeps_length(self):
        haystack = "a" * 1000
        msg = fail_message(assert_contains, haystack, "needle")
        self.assertIn("len 1000", msg)
        self.assertIn("truncated", msg)
        self.assertLess(len(msg), 500)

    def test_not_contains_fail(self):
        msg = fail_message(assert_not_contains, [1, 2], 2)
        self.assertIn("unexpectedly present: 2", msg)


class TestMessageStability(unittest.TestCase):
    """同一失败构造两次，输出必须逐字节一致。"""

    def assert_stable(self, build):
        first = fail_message(build)
        second = fail_message(build)
        self.assertEqual(first, second)

    def test_nested_diff_stable(self):
        def build():
            actual = {"b": [3, 1], "a": {"z": 1, "y": 2}}
            expected = {"a": {"y": 20, "z": 1}, "b": [1, 3, 9]}
            assert_equal(actual, expected)
        self.assert_stable(build)

    def test_set_diff_stable(self):
        def build():
            assert_equal({"gamma", "alpha", "delta"}, {"alpha", "beta", "gamma"})
        self.assert_stable(build)

    def test_sequence_alignment_stable(self):
        def build():
            assert_equal([5, 1, 2, 8, 3, 9], [1, 2, 3, 4, 7])
        self.assert_stable(build)

    def test_string_diff_stable(self):
        def build():
            assert_equal("z" * 300 + "A" + "z" * 300, "z" * 300 + "B" + "z" * 300)
        self.assert_stable(build)

    def test_almost_equal_stable(self):
        def build():
            assert_almost_equal(2.718281, 2.71828, tol=1e-6)
        self.assert_stable(build)

    def test_many_diffs_stable(self):
        def build():
            assert_equal({f"k{i}": i for i in range(40)},
                         {f"k{i}": -i for i in range(40)})
        self.assert_stable(build)


if __name__ == "__main__":
    unittest.main()
