"""entity_tool 修复版测试：五类缺陷回归 + 性质断言 + 策略一致性 + 边界用例。"""

import unittest

from entity_tool import EntityError, decode, encode


class TestFiveDefectsFixed(unittest.TestCase):
    """每一例对应一类已修复的现网缺陷。"""

    def test_no_double_escape(self):
        """缺陷1：已转义内容不再被二次转义。"""
        self.assertEqual(encode("&lt;"), "&lt;")
        self.assertEqual(encode("a &lt; b &amp; c"), "a &lt; b &amp; c")
        once = encode("x < y & z")
        self.assertEqual(encode(once), once)

    def test_incomplete_entity_rejected_or_preserved(self):
        """缺陷2：不完整实体不再被静默放行——strict 拒绝，lenient 显式保留。"""
        with self.assertRaises(EntityError):
            decode("a &lt b")
        with self.assertRaises(EntityError):
            encode("a &lt b")
        self.assertEqual(decode("a &lt b", strict=False), "a &lt b")
        self.assertEqual(encode("a &lt b", strict=False), "a &lt b")

    def test_out_of_range_numeric_entity(self):
        """缺陷3：越界与代理区数字实体被拒绝，报错含位置与原因。"""
        for bad in ("&#1114112;", "&#x110000;", "&#xD800;", "&#55296;", "&#xDFFF;"):
            with self.assertRaises(EntityError) as ctx:
                decode(bad)
            self.assertEqual(ctx.exception.position, 0)
            self.assertTrue(ctx.exception.reason)
        with self.assertRaises(EntityError) as ctx:
            decode("ok &#xD800;")
        self.assertEqual(ctx.exception.position, 3)
        self.assertIn("代理区", ctx.exception.reason)
        with self.assertRaises(EntityError) as ctx:
            decode("ok &#1114112;")
        self.assertIn("超出有效范围", ctx.exception.reason)

    def test_round_trip_restores_original(self):
        """缺陷4：往返可还原原文（含撇号）。"""
        for text in ("it's a test", 'say "hi" <now> & go', "plain text", "a & b"):
            self.assertEqual(decode(encode(text)), text)

    def test_case_insensitive_names(self):
        """缺陷5：实体名大小写不敏感。"""
        self.assertEqual(decode("&AMP;&Amp;&amp;"), "&&&")
        self.assertEqual(decode("&LT;&Lt;&lT;"), "<<<")
        self.assertEqual(decode("&QUOT;&quot;"), '""')
        self.assertEqual(decode("&#X41;&#x41;"), "AA")


class TestProperties(unittest.TestCase):
    """可断言性质。"""

    SAMPLES = [
        "",
        "hello world",
        "a < b & c > d",
        '"quoted" and \'apostrophe\'',
        "中文全角＆ｌｔ；混合 half-width",
        "&lt;already&gt;&amp;encoded",
        "line1\nline2\ttab",
        "&unknown preserved" ,
    ]

    def test_encode_idempotent(self):
        for s in self.SAMPLES:
            once = encode(s, strict=False)
            self.assertEqual(encode(once, strict=False), once, s)

    def test_encode_idempotent_strict_on_clean_text(self):
        for s in self.SAMPLES:
            if "&unknown" in s:
                continue  # strict 模式按策略拒绝非法实体
            once = encode(s)
            self.assertEqual(encode(once), once, s)

    def test_decode_encode_inverse_on_plain_text(self):
        """无预存实体的文本：decode(encode(s)) == s。"""
        for s in self.SAMPLES:
            if "&" in s:
                continue
            self.assertEqual(decode(encode(s)), s)

    def test_encode_decode_inverse_on_entities(self):
        """合法实体：解码后再编码再解码，内容不变；特殊字符实体可精确还原。"""
        entities = ["&lt;", "&gt;", "&amp;", "&quot;", "&apos;", "&#65;", "&#x4E2D;", "&copy;"]
        for e in entities:
            char = decode(e)
            self.assertEqual(decode(encode(char)), char, e)
        for e in ("&lt;", "&gt;", "&amp;", "&quot;", "&apos;"):
            self.assertEqual(encode(decode(e)), e)

    def test_case_insensitive_across_table(self):
        for name in ("amp", "lt", "gt", "quot", "apos", "copy", "reg", "hellip"):
            lower = decode(f"&{name};")
            self.assertEqual(decode(f"&{name.upper()};"), lower)
            self.assertEqual(decode(f"&{name.capitalize()};"), lower)


class TestPolicyConsistency(unittest.TestCase):
    """两种策略在 encode/decode 全流程行为一致。"""

    BAD_INPUTS = ["&foo;", "&lt", "&#xD800;", "&#99999999;", "&#;", "a &bogus; b"]

    def test_strict_rejects_everywhere(self):
        for bad in self.BAD_INPUTS:
            with self.assertRaises(EntityError, msg=f"decode {bad!r}"):
                decode(bad)
            with self.assertRaises(EntityError, msg=f"encode {bad!r}"):
                encode(bad)

    def test_lenient_preserves_everywhere(self):
        for bad in self.BAD_INPUTS:
            self.assertEqual(decode(bad, strict=False), bad)
            self.assertEqual(encode(bad, strict=False), bad)

    def test_error_carries_position_and_reason(self):
        with self.assertRaises(EntityError) as ctx:
            decode("abc &foo;")
        self.assertEqual(ctx.exception.position, 4)
        self.assertIn("未知实体名", ctx.exception.reason)
        self.assertIn("position 4", str(ctx.exception))
        with self.assertRaises(EntityError) as ctx:
            decode("x &lt")
        self.assertIn("缺少结束分号", ctx.exception.reason)

    def test_bare_ampersand_consistent(self):
        """裸露 & 非实体尝试：encode 转义、decode 放行，两种模式一致。"""
        self.assertEqual(encode("a & b"), "a &amp; b")
        self.assertEqual(encode("a & b", strict=False), "a &amp; b")
        self.assertEqual(decode("a & b"), "a & b")
        self.assertEqual(decode("a & b", strict=False), "a & b")


class TestBoundaries(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(encode(""), "")
        self.assertEqual(decode(""), "")

    def test_pure_entity(self):
        self.assertEqual(decode("&lt;"), "<")
        self.assertEqual(decode("&#65;"), "A")
        self.assertEqual(decode("&#x1F600;"), "\U0001F600")
        self.assertEqual(encode("<"), "&lt;")

    def test_nested_entity_single_pass(self):
        """嵌实体只解码一层，不递归展开。"""
        self.assertEqual(decode("&amp;lt;"), "&lt;")
        self.assertEqual(decode("&amp;amp;"), "&amp;")
        self.assertEqual(encode("&amp;lt;"), "&amp;lt;")

    def test_very_long_string(self):
        text = "ab&<>'\"中文" * 100_000  # 约 90 万字符
        encoded = encode(text)
        self.assertEqual(decode(encoded), text)
        self.assertEqual(encode(encoded), encoded)

    def test_mixed_full_half_width(self):
        """全角 ＆／＜ 不是实体语法，只有半角 & 触发实体处理。"""
        text = "全角＆＜＞与半角&<>混排"
        encoded = encode(text)
        self.assertIn("＆", encoded)  # 全角 ＆ 原样保留
        self.assertIn("&amp;", encoded)  # 半角 & 被转义
        self.assertEqual(decode(encoded), text)
        self.assertEqual(decode("＆ｌｔ；"), "＆ｌｔ；")  # 全角伪实体不受影响


if __name__ == "__main__":
    unittest.main()
