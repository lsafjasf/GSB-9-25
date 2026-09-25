import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from msgfmt import (ConfigError, MessageFormatter, MessageNotFoundError,
                    RenderError, TemplateSyntaxError)

ROOT = os.path.join(os.path.dirname(__file__), "..")


def make_formatter():
    return MessageFormatter.from_dir(
        os.path.join(ROOT, "config", "languages.json"),
        os.path.join(ROOT, "messages"))


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fmt = make_formatter()

    def render(self, locale, key, **args):
        return self.fmt.render(locale, key, args).text

    # -- same message, multiple languages -------------------------------

    def test_same_message_all_locales(self):
        args = {"user": "Ada", "count": 3, "total": 1234.5,
                "due": datetime.date(2026, 9, 30)}
        self.assertEqual(
            self.render("en", "cart.summary", **args),
            "Hi Ada, you have 3 items totaling 1,234.5, due on 09/30/2026.")
        self.assertEqual(
            self.render("fr", "cart.summary", **args),
            "Bonjour Ada, 3 articles pour un total de 1 234,5, "
            "à payer avant le 30/09/2026.")
        self.assertEqual(
            self.render("ru", "cart.summary", **args),
            "Ada, у вас 3 товара на сумму 1 234,5, оплатить до 30.09.2026.")
        # Japanese template reorders the placeholders (date before count).
        self.assertEqual(
            self.render("ja", "cart.summary", **args),
            "Ada さん：2026/09/30 までに合計 1,234.5、3 点 の商品があります。")

    # -- quantities: 0, 1, 1.5 across plural schemes --------------------

    def test_quantities_en(self):
        self.assertEqual(self.render("en", "files.selected", count=0),
                         "0 files selected")
        self.assertEqual(self.render("en", "files.selected", count=1),
                         "1 file selected")
        self.assertEqual(self.render("en", "files.selected", count=1.5),
                         "1.5 files selected")

    def test_quantities_fr_zero_is_singular(self):
        self.assertEqual(self.render("fr", "files.selected", count=0),
                         "0 fichier sélectionné")
        self.assertEqual(self.render("fr", "files.selected", count=1),
                         "1 fichier sélectionné")
        self.assertEqual(self.render("fr", "files.selected", count=1.5),
                         "1,5 fichiers sélectionnés")

    def test_quantities_ru_mod_rules(self):
        cases = {
            0: "Выбрано 0 файлов",      # many: n % 10 = 0
            1: "Выбран 1 файл",         # one
            1.5: "Выбрано 1,5 файла",   # other (non-integer)
            3: "Выбрано 3 файла",       # few
            5: "Выбрано 5 файлов",      # many
            11: "Выбрано 11 файлов",    # many: teens exception
            22: "Выбрано 22 файла",     # few
            111: "Выбрано 111 файлов",  # many: 111 % 100 = 11
        }
        for count, expected in cases.items():
            with self.subTest(count=count):
                self.assertEqual(
                    self.render("ru", "files.selected", count=count), expected)

    def test_quantities_ja_single_category(self):
        self.assertEqual(self.render("ja", "files.selected", count=0),
                         "0 件のファイルを選択しました")
        self.assertEqual(self.render("ja", "files.selected", count=1.5),
                         "1.5 件のファイルを選択しました")

    def test_plural_decision_is_explainable(self):
        result = self.fmt.render("ru", "files.selected", {"count": 5})
        trace = result.plural_trace[0]
        self.assertEqual(trace["category"], "many")
        evaluated = {e["category"]: e["result"] for e in trace["evaluations"]}
        self.assertFalse(evaluated["one"])
        self.assertFalse(evaluated["few"])
        self.assertTrue(evaluated["many"])
        explanation = result.explain()
        self.assertIn("plural(count=5) -> many", explanation)
        self.assertIn("n % 10 = 1", explanation)

    # -- localized numbers and dates from config ------------------------

    def test_number_formatting_per_locale(self):
        args = {"user": "A", "count": 1, "total": 1234567.5,
                "due": datetime.date(2026, 9, 30)}
        self.assertIn("1,234,567.5", self.render("en", "cart.summary", **args))
        self.assertIn("1 234 567,5", self.render("fr", "cart.summary", **args))
        self.assertIn("1 234 567,5", self.render("ru", "cart.summary", **args))

    def test_date_order_per_locale(self):
        args = {"user": "A", "count": 1, "total": 1,
                "due": datetime.date(2026, 9, 5)}
        self.assertIn("09/05/2026", self.render("en", "cart.summary", **args))
        self.assertIn("05/09/2026", self.render("fr", "cart.summary", **args))
        self.assertIn("05.09.2026", self.render("ru", "cart.summary", **args))
        self.assertIn("2026/09/05", self.render("ja", "cart.summary", **args))

    # -- fallback chain --------------------------------------------------

    def test_fallback_to_declared_locale(self):
        result = self.fmt.render("ja", "fallback.demo", {"user": "Ada"})
        self.assertEqual(result.locale_requested, "ja")
        self.assertEqual(result.locale_used, "en")
        self.assertTrue(result.fell_back)
        self.assertEqual(result.text,
                         "This message exists only in English, Ada.")

    def test_fallback_ru_to_en(self):
        result = self.fmt.render("ru", "fallback.demo", {"user": "Ada"})
        self.assertEqual(result.locale_used, "en")

    def test_no_fallback_needed(self):
        result = self.fmt.render("en", "files.selected", {"count": 2})
        self.assertEqual(result.locale_used, "en")
        self.assertFalse(result.fell_back)

    def test_missing_everywhere_raises(self):
        with self.assertRaises(MessageNotFoundError) as ctx:
            self.fmt.render("ja", "no.such.key")
        self.assertEqual(ctx.exception.key, "no.such.key")

    def test_unknown_locale_raises(self):
        with self.assertRaises(ConfigError):
            self.fmt.render("de", "files.selected", {"count": 1})

    # -- placeholder diagnostics -----------------------------------------

    def test_missing_extra_duplicate_args(self):
        template = "User {user} has {count} of {total} ({count} again)"
        result = self.fmt.render(
            "en", "diag.demo",
            {"user": "Ada", "count": 2, "junk": 1})
        by_code = {}
        for diag in result.diagnostics:
            by_code.setdefault(diag.code, []).append(diag)

        missing = by_code["missing-arg"][0]
        self.assertEqual(missing.name, "total")
        self.assertEqual(missing.pos, template.index("{total}"))

        extra = by_code["extra-arg"][0]
        self.assertEqual(extra.name, "junk")
        self.assertIsNone(extra.pos)

        duplicate = by_code["duplicate-arg"][0]
        self.assertEqual(duplicate.name, "count")
        self.assertEqual(duplicate.pos,
                         template.index("{count", template.index("{count}") + 1))

        # Missing placeholder renders as a literal, rendering never crashes.
        self.assertEqual(result.text,
                         "User Ada has 2 of {total} (2 again)")

    def test_validate_without_rendering(self):
        diags = self.fmt.validate("en", "diag.demo", {"user": "Ada"})
        codes = {d.code for d in diags}
        # "count" is missing AND used twice in the template.
        self.assertEqual(codes, {"missing-arg", "duplicate-arg"})

    def test_strict_mode_raises_on_diagnostics(self):
        with self.assertRaises(RenderError):
            self.fmt.render("en", "diag.demo", {"user": "Ada"}, strict=True)

    def test_clean_render_has_no_diagnostics(self):
        result = self.fmt.render("en", "files.selected", {"count": 2})
        self.assertEqual(result.diagnostics, [])

    # -- template syntax errors ------------------------------------------

    def test_template_syntax_errors(self):
        bad = ["Hello {", "Hello }", "{count, plural}", "{count, bogus}",
               "{count, plural, one {x}", "{1abc}"]
        for template in bad:
            with self.subTest(template=template):
                fmt = make_formatter()
                fmt.catalogs["en"] = {"bad": template}
                with self.assertRaises(TemplateSyntaxError) as ctx:
                    fmt.render("en", "bad")
                self.assertIsInstance(ctx.exception.pos, int)


class ConfigValidationTests(unittest.TestCase):
    def base_config(self):
        return {
            "locales": {
                "en": {
                    "fallback": [],
                    "number": {"thousands_sep": ",", "decimal_sep": ".",
                               "decimal_places": None},
                    "date": {"order": "MDY", "separator": "/", "pad": True},
                    "plural": {"categories": [
                        {"name": "one", "when": "n = 1"},
                        {"name": "other", "when": None},
                    ]},
                },
            },
        }

    def build(self, config):
        return MessageFormatter(config, {"en": {"k": "v"}})

    def mutate_plural(self, config, categories):
        config["locales"]["en"]["plural"]["categories"] = categories
        return config

    def test_valid_config_loads(self):
        self.build(self.base_config())

    def test_bad_plural_expression(self):
        config = self.mutate_plural(self.base_config(), [
            {"name": "one", "when": "n % % 1"},
            {"name": "other", "when": None},
        ])
        with self.assertRaises(ConfigError):
            self.build(config)

    def test_unknown_identifier_in_rule(self):
        config = self.mutate_plural(self.base_config(), [
            {"name": "one", "when": "m = 1"},
            {"name": "other", "when": None},
        ])
        with self.assertRaises(ConfigError):
            self.build(config)

    def test_missing_catch_all(self):
        config = self.mutate_plural(self.base_config(), [
            {"name": "one", "when": "n = 1"},
            {"name": "other", "when": "n != 1"},
        ])
        with self.assertRaises(ConfigError):
            self.build(config)

    def test_catch_all_must_be_named_other(self):
        config = self.mutate_plural(self.base_config(), [
            {"name": "one", "when": "n = 1"},
            {"name": "rest", "when": None},
        ])
        with self.assertRaises(ConfigError):
            self.build(config)

    def test_duplicate_category_names(self):
        config = self.mutate_plural(self.base_config(), [
            {"name": "one", "when": "n = 1"},
            {"name": "one", "when": "n = 2"},
            {"name": "other", "when": None},
        ])
        with self.assertRaises(ConfigError):
            self.build(config)

    def test_bad_date_order(self):
        config = self.base_config()
        config["locales"]["en"]["date"]["order"] = "YMX"
        with self.assertRaises(ConfigError):
            self.build(config)

    def test_bad_decimal_places(self):
        config = self.base_config()
        config["locales"]["en"]["number"]["decimal_places"] = -1
        with self.assertRaises(ConfigError):
            self.build(config)

    def test_fallback_to_undeclared_locale(self):
        config = self.base_config()
        config["locales"]["en"]["fallback"] = ["de"]
        with self.assertRaises(ConfigError):
            self.build(config)

    def test_missing_sections(self):
        for section in ("number", "date", "plural"):
            config = self.base_config()
            del config["locales"]["en"][section]
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    self.build(config)


if __name__ == "__main__":
    unittest.main()
