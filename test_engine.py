"""regex_engine 单元测试：python3 -m unittest test_engine -v"""
import unittest

import regex_engine as rx


class TestMatch(unittest.TestCase):
    def assertMatch(self, pattern, text):
        self.assertTrue(rx.search(pattern, text), f"{pattern!r} 应匹配 {text!r}")

    def assertNoMatch(self, pattern, text):
        self.assertFalse(rx.search(pattern, text), f"{pattern!r} 不应匹配 {text!r}")

    def test_literal(self):
        self.assertMatch('abc', 'xxabcxx')
        self.assertNoMatch('abc', 'abd')
        self.assertMatch('', '')
        self.assertMatch('', '任意')

    def test_empty_input(self):
        self.assertNoMatch('a', '')
        self.assertMatch('a*', '')
        self.assertMatch('^$', '')
        self.assertNoMatch('^a', '')

    def test_dot(self):
        self.assertMatch('a.c', 'abc')
        self.assertNoMatch('a.c', 'a\nc')  # 非 DOTALL
        self.assertMatch('.', 'x')
        self.assertNoMatch('.', '\n')

    def test_class(self):
        self.assertMatch('[abc]', 'xb')
        self.assertNoMatch('[abc]', 'xyz')
        self.assertMatch('[^abc]', 'xyz')
        self.assertNoMatch('[^abc]', 'cba')
        self.assertMatch('[a-z]+', 'hello')
        self.assertNoMatch('^[a-z]+$', 'Hello')
        self.assertMatch('[a-cb-d]', 'd')       # 重复/相邻区间
        self.assertMatch('[a-ca-c]', 'b')       # 重复区间
        self.assertMatch('[]a]', ']')           # 首个 ] 为字面量
        self.assertMatch('[^]a]', 'b')
        self.assertNoMatch('[^]a]', ']')
        self.assertMatch('[a-]', '-')           # 末尾 - 为字面量
        self.assertMatch('[-a]', '-')
        self.assertMatch('[--0]', '.')          # 区间 '-' 到 '0'
        self.assertMatch('[\d]', '5')
        self.assertNoMatch('[\D]', '5')
        self.assertMatch('[\]]', ']')
        self.assertMatch('[a^]', '^')           # 非首位 ^ 为字面量

    def test_quantifier(self):
        self.assertMatch('ab*c', 'ac')
        self.assertMatch('ab*c', 'abbbc')
        self.assertMatch('ab+c', 'abc')
        self.assertNoMatch('ab+c', 'ac')
        self.assertMatch('ab?c', 'ac')
        self.assertNoMatch('ab?c', 'abbc')
        self.assertMatch('a*?b', 'aab')         # 惰性后缀
        self.assertMatch('a{2,3}', 'aa')
        self.assertNoMatch('^a{2,3}$', 'a')
        self.assertMatch('^a{2,3}$', 'aaa')
        self.assertNoMatch('^a{2,3}$', 'aaaa')
        self.assertMatch('^a{2}$', 'aa')
        self.assertMatch('^a{2,}$', 'aaaa')
        self.assertMatch('^a{,2}$', 'a')
        self.assertNoMatch('^a{,2}$', 'aaa')
        self.assertMatch('a{', 'a{')            # 非法重复按字面量
        self.assertMatch('a{}', 'a{}')
        self.assertMatch('a*{', 'a{')

    def test_escape(self):
        self.assertMatch(r'\.', '.')
        self.assertNoMatch(r'\.', 'x')
        self.assertMatch(r'\d+', 'abc123')
        self.assertMatch(r'\w+', 'hello_1')
        self.assertMatch(r'\s', 'a b')
        self.assertMatch(r'\n', 'a\nb')
        self.assertMatch(r'\*', 'a*b')
        self.assertMatch(r'\\', 'a\\b')

    def test_anchor(self):
        self.assertMatch('^abc', 'abcdef')
        self.assertNoMatch('^abc', 'xabc')
        self.assertMatch('abc$', 'xabc')
        self.assertNoMatch('abc$', 'abcx')
        self.assertMatch('^abc$', 'abc')
        self.assertNoMatch('^abc$', 'abcabc')
        self.assertMatch('a$', 'a\n')           # $ 匹配结尾换行之前
        self.assertNoMatch('a$', 'a\n\n')
        self.assertMatch('^$', '\n')
        self.assertNoMatch('a^b', 'a^b')        # 中间 ^ 仍是断言
        self.assertMatch('^^a', 'a')

    def test_long_input(self):
        text = 'a' * 1_000_000
        self.assertNoMatch('a+b', text)
        self.assertMatch('a+b', text + 'b')
        self.assertMatch('[0-9]+x', 'a' * 500_000 + '123x')

    def test_pathological(self):
        # 对回溯实现是 O(2^n)，NFA 模拟应瞬间完成
        n = 200
        self.assertMatch('a?' * n + 'a' * n, 'a' * n)
        self.assertNoMatch('.*.*.*.*b', 'a' * 5000)


class TestSyntaxError(unittest.TestCase):
    def assertError(self, pattern, pos, keyword):
        with self.assertRaises(rx.RegexSyntaxError) as ctx:
            rx.compile(pattern)
        err = ctx.exception
        self.assertEqual(err.pos, pos, f"{pattern!r} 错误位置")
        self.assertIn(keyword, err.reason, f"{pattern!r} 错误原因")

    def test_unclosed_class(self):
        self.assertError('[abc', 0, '未闭合')
        self.assertError('ab[', 2, '未闭合')
        self.assertError('[]', 0, '未闭合')

    def test_dangling_escape(self):
        self.assertError('a\\', 1, '悬空转义')
        self.assertError('\\', 0, '悬空转义')
        self.assertError('[a\\', 2, '悬空转义')

    def test_quantifier_without_operand(self):
        self.assertError('*abc', 0, '缺少操作数')
        self.assertError('+', 0, '缺少操作数')
        self.assertError('a^*', 2, '缺少操作数')
        self.assertError('{2}a', 0, '缺少操作数')

    def test_multiple_quantifier(self):
        self.assertError('a**', 2, '多重量词')
        self.assertError('a+?*', 3, '多重量词')
        self.assertError('a{2}{3}', 4, '多重量词')

    def test_bad_range(self):
        self.assertError('[z-a]', 1, '反序')
        self.assertError('[a-\\d]', 1, '预定义字符类')
        self.assertError('[\\d-z]', 1, '预定义字符类')

    def test_bad_brace(self):
        self.assertError('a{3,2}', 1, '反序')
        self.assertError('a{10001}', 1, '上限')

    def test_unknown_escape(self):
        self.assertError('\\q', 0, '未知转义')
        self.assertError('[\\q]', 1, '未知转义')

    def test_lazy_is_ok(self):
        rx.compile('a*?')
        rx.compile('a+?b')
        rx.compile('a{1,3}?')

    def test_assertion_escapes(self):
        self.assertTrue(rx.search(r'\Aabc', 'abc'))
        self.assertFalse(rx.search(r'\Aabc', 'xabc'))
        self.assertTrue(rx.search(r'abc\Z', 'xabc'))
        self.assertFalse(rx.search(r'abc\Z', 'abc\n'))  # \\Z 不容结尾换行
        self.assertTrue(rx.search(r'\babc\b', 'x abc y'))
        self.assertFalse(rx.search(r'\babc\b', 'xabcy'))
        self.assertTrue(rx.search(r'\B', 'ab'))
        self.assertTrue(rx.search(r'\a', '\x07'))
        self.assertTrue(rx.search(r'\0', '\x00'))

    def test_possessive_suffix_rejected(self):
        # 占有量词需要提交语义，本引擎明确拒绝而非给出错误结果
        for pat in ('a*+', 'a++', 'a?+', 'a{2}+'):
            with self.assertRaises(rx.RegexSyntaxError, msg=pat):
                rx.compile(pat)
        with self.assertRaises(rx.RegexSyntaxError):
            rx.compile(r'\b*')


if __name__ == '__main__':
    unittest.main()
