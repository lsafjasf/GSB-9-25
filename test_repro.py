"""test_repro.py —— 稳定复现四类现网缺陷，并验证修复后行为。

每个用例：同一份输入，naive_parser（修复前）给出错误结果（缺陷复现），
dsv_parser（修复后）给出正确结果。
"""

import unittest

import naive_parser
import dsv_parser


class TestBug1DelimiterInsideField(unittest.TestCase):
    """字段内含分隔符时被错误切开。"""

    TEXT = 'name,desc\nalice,"likes, commas"\nbob,"a,b,c"\n'
    EXPECTED = [["name", "desc"], ["alice", "likes, commas"], ["bob", "a,b,c"]]

    def test_bug_reproduced_on_naive(self):
        self.assertNotEqual(naive_parser.parse(self.TEXT), self.EXPECTED)

    def test_fixed(self):
        self.assertEqual(dsv_parser.parse(self.TEXT).rows, self.EXPECTED)


class TestBug2NewlineInsideQuotes(unittest.TestCase):
    """引号内换行被当成记录结束。"""

    TEXT = 'id,note\n1,"line one\nline two"\n2,"single"\n'
    EXPECTED = [["id", "note"], ["1", "line one\nline two"], ["2", "single"]]

    def test_bug_reproduced_on_naive(self):
        self.assertNotEqual(naive_parser.parse(self.TEXT), self.EXPECTED)

    def test_fixed(self):
        self.assertEqual(dsv_parser.parse(self.TEXT).rows, self.EXPECTED)


class TestBug3EscapedQuotes(unittest.TestCase):
    """转义引号（双写）处理错误。"""

    TEXT = 'id,quote\n1,"say ""hi"""\n2,"""lead and trail"""\n'
    EXPECTED = [["id", "quote"], ["1", 'say "hi"'], ["2", '"lead and trail"']]

    def test_bug_reproduced_on_naive(self):
        self.assertNotEqual(naive_parser.parse(self.TEXT), self.EXPECTED)

    def test_fixed(self):
        self.assertEqual(dsv_parser.parse(self.TEXT).rows, self.EXPECTED)


class TestBug4SingleLineInference(unittest.TestCase):
    """只有一行数据时推断结果与多行不一致。"""

    ONE_LINE = '"a,b";"c,d"'
    MULTI_LINE = '"a,b";"c,d"\n"e,f";"g,h"\n'
    EXPECTED_SINGLE = [["a,b", "c,d"]]

    def test_bug_reproduced_on_naive(self):
        # 缺陷：naive 对单行走 raw-count 兜底，误判为逗号
        naive_single = naive_parser.infer(self.ONE_LINE)[0]
        naive_multi = naive_parser.infer(self.MULTI_LINE)[0]
        self.assertNotEqual(naive_single, naive_multi)
        self.assertNotEqual(naive_parser.parse(self.ONE_LINE), self.EXPECTED_SINGLE)

    def test_fixed_consistent(self):
        single = dsv_parser.infer(self.ONE_LINE)
        multi = dsv_parser.infer(self.MULTI_LINE)
        self.assertEqual(single.delimiter, multi.delimiter)
        self.assertEqual(single.quotechar, multi.quotechar)
        self.assertEqual(dsv_parser.parse(self.ONE_LINE).rows, self.EXPECTED_SINGLE)


if __name__ == "__main__":
    unittest.main()
