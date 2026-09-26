# -*- coding: utf-8 -*-
"""序列化修复的复现用例与回归测试。

  BuggyReproTests     —— 修复前：稳定复现旧实现（serializer_buggy）的五类缺陷
  FixedRoundTripTests —— 修复后：serializer（格式 v2）的往返等价断言
  MalformedInputTests —— 修复后：非法输入的字节偏移与期望结构
  CompatTests         —— 兼容性边界：v1 历史数据可读、v2 不被旧代码接受

运行: python3 -m unittest test_serializer -v   （或 python3 test_serializer.py）
"""

import unittest

import serializer
import serializer_buggy as buggy

EMOJI = "\U0001F600"          # 😀
MUSICAL = "\U0001D11E"        # 𝄞
ORIGINAL_STR = ('路径 C:\\new\\dir，多字节字符「中文' + EMOJI +
                '」，引号"与换行\n')


class BuggyReproTests(unittest.TestCase):
    """修复前对照组：每个用例稳定复现一类现网缺陷。"""

    def test_repro1_string_content_changed(self):
        # 缺陷1: 含多字节字符与转义字符的字符串，解析后内容改变
        restored = buggy.loads(buggy.dumps(ORIGINAL_STR))
        self.assertNotEqual(restored, ORIGINAL_STR)  # 缺陷复现：往返不等价

    def test_repro2_big_int_precision_loss(self):
        # 缺陷2: 超出 float53 精度的整数被截断
        original = 2 ** 60 + 1
        restored = buggy.loads(buggy.dumps(original))
        self.assertNotEqual(restored, original)  # 缺陷复现：精度丢失

    def test_repro3_shared_reference_duplicated(self):
        # 缺陷3: 同一对象被多处引用，序列化后变成多份拷贝
        shared = {"k": 1}
        restored = buggy.loads(buggy.dumps([shared, shared]))
        self.assertIsNot(restored[0], restored[1])  # 缺陷复现：共享关系丢失

    def test_repro4_cycle_stack_overflow(self):
        # 缺陷4: 循环引用无限递归直至栈溢出
        obj = []
        obj.append(obj)
        with self.assertRaises(RecursionError):
            buggy.dumps(obj)

    def test_repro5_error_has_no_location(self):
        # 缺陷5: 非法输入只报一句「解析失败」，无位置无原因
        with self.assertRaises(ValueError) as ctx:
            buggy.loads('[1,]')
        self.assertEqual(str(ctx.exception), "parse failed")


class FixedRoundTripTests(unittest.TestCase):
    """修复后：往返必须在值与共享关系上都等价。"""

    def roundtrip(self, obj):
        return serializer.loads(serializer.dumps(obj))

    def test_special_strings_unchanged(self):
        cases = [
            "",
            "多字节字符：中文、日文テスト、emoji " + EMOJI,
            "转义字符：\\ \" \n \t \r \0 结尾",
            "\\u4e2d 字面值（反斜杠+u 不应被解释）",
            '混合 "引号" \\ 反斜杠 \n 换行「中文」' + EMOJI,
            "高位平面字符 " + MUSICAL,
            "包含格式分隔符 ; : 的字符串",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(self.roundtrip(text), text)

    def test_big_int_exact(self):
        for num in [0, -1, 2 ** 53 - 1, 2 ** 53, 2 ** 53 + 1,
                    2 ** 60 + 1, -(2 ** 80), 10 ** 100 + 7]:
            with self.subTest(num=num):
                restored = self.roundtrip(num)
                self.assertEqual(restored, num)
                self.assertIsInstance(restored, int)

    def test_float_roundtrip(self):
        for num in [0.0, -1.5, 1e300, 5e-324, float("inf"), float("-inf")]:
            with self.subTest(num=num):
                self.assertEqual(self.roundtrip(num), num)

    def test_shared_reference_preserved(self):
        shared = {"v": [1, 2]}
        obj = [shared, shared, {"again": shared}]
        result = self.roundtrip(obj)
        self.assertIs(result[0], result[1])          # 共享引用不得变成拷贝
        self.assertIs(result[0], result[2]["again"])
        result[0]["v"].append(3)                      # 改一处，处处可见
        self.assertEqual(result[1]["v"], [1, 2, 3])
        self.assertEqual(result[2]["again"]["v"], [1, 2, 3])

    def test_cycle_supported_no_stack_overflow(self):
        obj = []
        obj.append(obj)                    # 自引用
        obj.append({"self": obj})          # 经 dict 的间接环
        result = self.roundtrip(obj)
        self.assertIs(result[0], result)
        self.assertIs(result[1]["self"], result)

    def test_mutual_cycle_supported(self):
        alice = {"name": "a"}
        bob = {"name": "b", "friend": alice}
        alice["friend"] = bob
        result = self.roundtrip(alice)
        self.assertIs(result["friend"]["friend"], result)

    def test_deep_nesting_rejected_without_recursion_error(self):
        # 深度超限必须抛业务异常，而不是 RecursionError / 栈溢出
        obj = []
        cursor = obj
        for _ in range(serializer.MAX_DEPTH + 50):
            nxt = []
            cursor.append(nxt)
            cursor = nxt
        with self.assertRaises(serializer.EncodeError):
            serializer.dumps(obj)
        deep_data = b"L1;" * (serializer.MAX_DEPTH + 50) + b"N"
        with self.assertRaises(serializer.ParseError):
            serializer.loads(deep_data)

    def test_encode_rejects_unsupported(self):
        with self.assertRaises(serializer.EncodeError):
            serializer.dumps({1: "非 str 键"})
        with self.assertRaises(serializer.EncodeError):
            serializer.dumps(object())


class MalformedInputTests(unittest.TestCase):
    """修复后：非法输入必须报出字节偏移与期望结构。"""

    def assert_parse_error(self, data, offset, expected_part):
        with self.assertRaises(serializer.ParseError) as ctx:
            serializer.loads(data)
        err = ctx.exception
        self.assertEqual(err.offset, offset)
        self.assertIn(expected_part, err.expected)
        self.assertIn("byte offset %d" % offset, str(err))
        return err

    def test_unknown_tag(self):
        # 首字节非 v2 标签会按 v1 旧格式解析；v2 上下文内的非法标签报 v2 错误
        self.assert_parse_error(b"X", 0, "value (string/number/array/object/literal)")
        self.assert_parse_error(b"L1;X", 3, "value tag")

    def test_bad_integer(self):
        self.assert_parse_error(b"I12x;", 1, "integer digits")

    def test_truncated_list(self):
        self.assert_parse_error(b"L2;N", 4, "value tag")

    def test_truncated_string_payload(self):
        err = self.assert_parse_error(b"S5:ab", 3, "5 bytes of string payload")
        self.assertIn("2 bytes remaining", str(err.found))

    def test_bad_string_length(self):
        self.assert_parse_error(b"Sx:abc", 1, "string byte length")

    def test_dangling_reference(self):
        self.assert_parse_error(b"R0;", 1, "reference id < 0")

    def test_trailing_garbage(self):
        self.assert_parse_error(b"NN", 1, "end of input")

    def test_empty_input(self):
        self.assert_parse_error(b"", 0, "value tag")

    def test_invalid_utf8(self):
        # 偏移指向字符串负载中第一个非法字节
        err = self.assert_parse_error(b"S1:\xff", 3, "valid UTF-8")
        self.assertEqual(err.found, b"\xff")


class CompatTests(unittest.TestCase):
    """兼容性边界（详见 FORMAT.md）。"""

    def test_legacy_v1_data_readable(self):
        # 历史数据（未触发旧实现写出缺陷的部分）仍可读
        legacy = '[1.0,2.0,"abc",null,true,[3.0],{"k":"v"}]'
        result = serializer.loads(legacy)
        self.assertEqual(result, [1.0, 2.0, "abc", None, True, [3.0], {"k": "v"}])

    def test_legacy_unicode_escape_recovered(self):
        # 旧实现把非 ASCII 写成 \uXXXX，兼容读取器可还原
        self.assertEqual(serializer.loads('"\\u4e2d\\u6587"'), "中文")

    def test_legacy_malformed_also_reports_offset(self):
        with self.assertRaises(serializer.ParseError) as ctx:
            serializer.loads(b"[1,]")
        self.assertGreaterEqual(ctx.exception.offset, 0)
        self.assertIn("byte offset", str(ctx.exception))

    def test_new_format_not_readable_by_legacy_code(self):
        # 反向不兼容：旧代码读不了 v2 数据（边界，需灰度/双写）
        data = serializer.dumps({"a": 1})
        with self.assertRaises(ValueError):
            buggy.loads(data.decode("latin1"))


if __name__ == "__main__":
    unittest.main()
