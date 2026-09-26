# -*- coding: utf-8 -*-
"""msgfmt 自测：多语言渲染、数量 0/1/1.5、回退、占位符校验、非法配置。"""
import copy
import datetime
import json
import unittest

import msgfmt
from msgfmt import (ConfigError, Formatter, MessageFormatError, MissingMessageError,
                    decide_plural, format_date, format_number)


def load_formatter():
    with open("locales.json", encoding="utf-8") as f:
        locales = json.load(f)
    with open("messages.json", encoding="utf-8") as f:
        messages = json.load(f)
    return Formatter(locales, messages), locales, messages


class TestRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fmt, cls.locales, cls.messages = load_formatter()

    def test_same_message_multi_locale(self):
        args = dict(user="Ana", count=2)
        self.assertEqual(self.fmt.format("en", "files.selected", **args).text,
                         "Ana selected 2 files")
        self.assertEqual(self.fmt.format("zh", "files.selected", **args).text,
                         "Ana 已选择 2 个文件")
        self.assertEqual(self.fmt.format("ru", "files.selected", **args).text,
                         "Ana выбрал(а) 2 файла")
        self.assertEqual(self.fmt.format("fr", "files.selected", **args).text,
                         "Ana a sélectionné 2 fichiers")

    def test_placeholder_reorder_per_locale(self):
        # 同一组参数，各语言模板顺序不同，渲染都正确
        args = dict(user="Bo", count=3, date=datetime.date(2026, 9, 27))
        en = self.fmt.format("en", "inbox.summary", **args).text
        zh = self.fmt.format("zh", "inbox.summary", **args).text
        self.assertEqual(en, "Hello Bo, you have 3 messages since 09/27/2026.")
        self.assertEqual(zh, "Bo，自 2026-09-27 以来你有 3 条消息。")

    def test_counts_zero_one_fraction(self):
        self.assertIn("0 files", self.fmt.format("en", "files.selected", user="A", count=0).text)
        self.assertIn("1 file", self.fmt.format("en", "files.selected", user="A", count=1).text)
        r = self.fmt.format("en", "files.selected", user="A", count=1.5)
        self.assertIn("1.5 files", r.text)
        self.assertEqual(r.plurals[0].category, "other")
        # 中文不区分单复数：0/1/1.5 同一句式
        for n in (0, 1, 1.5):
            self.assertIn("个文件", self.fmt.format("zh", "files.selected", user="A", count=n).text)

    def test_russian_plural_buckets(self):
        cases = {1: "one", 2: "few", 5: "many", 11: "many", 21: "one",
                 111: "many", 0: "many", 1.5: "other"}
        for n, cat in cases.items():
            r = self.fmt.format("ru", "files.selected", user="A", count=n)
            self.assertEqual(r.plurals[0].category, cat, "n=%r" % n)

    def test_plural_explainable(self):
        r = self.fmt.format("ru", "files.selected", user="A", count=2)
        exp = r.plurals[0].explanation
        self.assertIn("n=2", exp)
        self.assertIn("few", exp)
        self.assertIn("n % 10 in [2, 3, 4]", exp)

    def test_number_and_date_from_config(self):
        en_num = self.locales["en"]["number"]
        fr_num = self.locales["fr"]["number"]
        self.assertEqual(format_number(1234567.5, en_num), "1,234,567.5")
        self.assertEqual(format_number(1234567.5, fr_num), "1 234 567,5")
        self.assertEqual(format_number(1000000, en_num), "1,000,000")
        self.assertEqual(format_number(-42.25, fr_num), "-42,25")
        d = datetime.date(2026, 9, 7)
        self.assertEqual(format_date(d, self.locales["en"]["date"]), "09/07/2026")
        self.assertEqual(format_date(d, self.locales["zh"]["date"]), "2026-09-07")
        self.assertEqual(format_date(d, self.locales["ru"]["date"]), "07.09.2026")

    def test_number_format_follows_locale_actually_used(self):
        # fr 回退到 en 的消息时，数字按实际使用的 en 配置格式化
        r = self.fmt.format("fr", "quota.notice", user="Ana", limit=1500.5)
        self.assertEqual(r.locale_used, "en")
        self.assertIn("1,500.5", r.text)


class TestFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fmt, _, _ = load_formatter()

    def test_fallback_marks_actual_locale(self):
        r = self.fmt.format("fr", "quota.notice", user="Ana", limit=10)
        self.assertEqual(r.requested_locale, "fr")
        self.assertEqual(r.locale_used, "en")
        self.assertEqual(r.chain, ["fr", "en"])

    def test_no_fallback_needed(self):
        r = self.fmt.format("fr", "files.selected", user="Ana", count=1)
        self.assertEqual(r.locale_used, "fr")

    def test_zh_falls_back_to_en(self):
        r = self.fmt.format("zh", "quota.notice", user="Ana", limit=5)
        self.assertEqual(r.locale_used, "en")
        self.assertEqual(r.text, "Limit 5 reached for Ana")

    def test_missing_everywhere_raises(self):
        with self.assertRaises(MissingMessageError):
            self.fmt.format("fr", "no.such.key", user="A")


class TestPlaceholderValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fmt, _, cls.messages = load_formatter()

    def test_missing_argument_reports_name_and_position(self):
        with self.assertRaises(MessageFormatError) as cm:
            self.fmt.format("en", "files.selected", count=2)
        msg = str(cm.exception)
        self.assertIn("'user'", msg)
        self.assertIn("position 0", msg)

    def test_extra_argument_reported(self):
        with self.assertRaises(MessageFormatError) as cm:
            self.fmt.format("en", "welcome", user="A", ghost=1)
        self.assertIn("'ghost'", str(cm.exception))

    def test_duplicate_placeholder_detected_with_positions(self):
        fmt = Formatter(self.fmt.locales, {"en": {"dup": "{a} and {b} and {a}"}})
        r = fmt.format("en", "dup", a="x", b="y")
        self.assertEqual(r.text, "x and y and x")
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("'a'", r.warnings[0])
        self.assertIn("0", r.warnings[0])  # 首次出现位置

    def test_template_syntax_errors(self):
        with self.assertRaises(MessageFormatError):
            msgfmt.parse_template("hello {")
        with self.assertRaises(MessageFormatError):
            msgfmt.parse_template("hello }")
        with self.assertRaises(MessageFormatError):
            msgfmt.parse_template("{n, plural, one{x} one{y} other{z}}")


class TestInvalidConfig(unittest.TestCase):
    def base(self):
        _, locales, messages = load_formatter()
        return copy.deepcopy(locales), messages

    def test_missing_other_category(self):
        locales, messages = self.base()
        locales["en"]["plural_rules"] = [{"category": "one", "when": [{"op": "eq", "value": 1}]}]
        with self.assertRaises(ConfigError):
            Formatter(locales, messages)

    def test_unknown_op(self):
        locales, messages = self.base()
        locales["en"]["plural_rules"][0]["when"] = [{"op": "magic"}]
        with self.assertRaises(ConfigError):
            Formatter(locales, messages)

    def test_no_catchall(self):
        locales, messages = self.base()
        locales["en"]["plural_rules"] = [
            {"category": "one", "when": [{"op": "eq", "value": 1}]},
            {"category": "other", "when": [{"op": "eq", "value": 2}]},
        ]
        with self.assertRaises(ConfigError):
            Formatter(locales, messages)

    def test_bad_date_order(self):
        locales, messages = self.base()
        locales["en"]["date"]["order"] = ["year", "year", "day"]
        with self.assertRaises(ConfigError):
            Formatter(locales, messages)

    def test_bad_number_group(self):
        locales, messages = self.base()
        locales["en"]["number"]["group"] = 0
        with self.assertRaises(ConfigError):
            Formatter(locales, messages)

    def test_unknown_fallback_target(self):
        locales, messages = self.base()
        locales["en"]["fallback"] = ["klingon"]
        with self.assertRaises(ConfigError):
            Formatter(locales, messages)


class TestPluralDecide(unittest.TestCase):
    def test_decision_is_explainable(self):
        rules = [{"category": "one", "when": [{"op": "eq", "value": 1}]},
                 {"category": "other", "when": [{"op": "always"}]}]
        d = decide_plural(1, rules)
        self.assertEqual(d.category, "one")
        self.assertIn("n == 1", d.explanation)
        d = decide_plural(0, rules)
        self.assertEqual(d.category, "other")
        self.assertIn("always", d.explanation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
