import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timespan import Locale, LocaleConfigError, TimeSpanFormatter, load_locale

LOCALES = Path(__file__).resolve().parent.parent / "timespan" / "locales"


def make_zh():
    return TimeSpanFormatter.from_locale_file(LOCALES / "zh.json")


def make_en():
    return TimeSpanFormatter.from_locale_file(LOCALES / "en.json")


MIN, HOUR, DAY = 60, 3600, 86400
WEEK, MONTH, YEAR = 604800, 2629800, 31557600


class TestZeroAndBelowMin(unittest.TestCase):
    def setUp(self):
        self.zh = make_zh()
        self.en = make_en()

    def test_zero_interval_uses_below_min_rule(self):
        self.assertEqual(self.zh.format(0), "刚刚")
        self.assertEqual(self.en.format(0), "just now")

    def test_below_smallest_unit(self):
        self.assertEqual(self.zh.format(0.4), "刚刚")
        self.assertEqual(self.en.format(-0.4), "just now")

    def test_below_min_zero_mode_renders_zero_smallest_unit(self):
        config = {
            "plural_rule": "one_other",
            "component_template": "{value} {unit}",
            "past": "{span} ago",
            "future": "in {span}",
            "below_min_unit": {"mode": "zero"},
            "units": [
                {"id": "minute", "seconds": 60,
                 "forms": {"one": "minute", "other": "minutes"}},
                {"id": "second", "seconds": 1,
                 "forms": {"one": "second", "other": "seconds"}},
            ],
        }
        fmt = TimeSpanFormatter(Locale(config, source="test://zero-mode"))
        self.assertEqual(fmt.format(0), "0 seconds")
        self.assertEqual(fmt.format(0.9), "0 seconds")
        self.assertEqual(fmt.format(-0.9), "0 seconds")


class TestExactUnitsAndDirection(unittest.TestCase):
    def setUp(self):
        self.zh = make_zh()
        self.en = make_en()

    def test_exact_units(self):
        self.assertEqual(self.zh.format(1), "1秒后")
        self.assertEqual(self.zh.format(MIN), "1分钟后")
        self.assertEqual(self.zh.format(HOUR), "1小时后")
        self.assertEqual(self.zh.format(DAY), "1天后")
        self.assertEqual(self.zh.format(WEEK), "1周后")
        self.assertEqual(self.zh.format(MONTH), "1个月后")
        self.assertEqual(self.zh.format(YEAR), "1年后")

    def test_direction_from_sign(self):
        self.assertEqual(self.zh.format(-HOUR), "1小时前")
        self.assertEqual(self.zh.format(HOUR), "1小时后")
        self.assertEqual(self.en.format(-90, precision=1), "1 minute ago")
        self.assertEqual(self.en.format(90, precision=1), "in 1 minute")

    def test_english_plurals(self):
        self.assertEqual(self.en.format(1), "in 1 second")
        self.assertEqual(self.en.format(2), "in 2 seconds")
        self.assertEqual(self.en.format(-YEAR), "1 year ago")
        self.assertEqual(self.en.format(2 * YEAR), "in 2 years")

    def test_precision_keeps_most_significant(self):
        self.assertEqual(self.zh.format(HOUR + 2 * MIN + 3, precision=1), "1小时后")
        self.assertEqual(self.zh.format(HOUR + 2 * MIN + 3, precision=2), "1小时2分钟后")
        self.assertEqual(self.zh.format(HOUR + 2 * MIN + 3, precision=3), "1小时2分钟3秒后")
        self.assertEqual(self.en.format(HOUR + 2 * MIN + 3, precision=2), "in 1 hour 2 minutes")


class TestCarryBoundaries(unittest.TestCase):
    """The core anti-'60 minutes' guarantees."""

    def setUp(self):
        self.zh = make_zh()
        self.en = make_en()

    def test_truncate_never_carries(self):
        self.assertEqual(self.zh.format(59 * MIN + 59, precision=1), "59分钟后")
        self.assertEqual(self.zh.format(23 * HOUR + 59 * MIN + 59, precision=1), "23小时后")
        self.assertEqual(self.zh.format(6 * DAY + 23 * HOUR, precision=1), "6天后")
        self.assertEqual(self.zh.format(11 * MONTH + 29 * DAY, precision=1), "11个月后")

    def test_truncate_full_precision_boundary(self):
        self.assertEqual(
            self.zh.format(59 * MIN + 59, precision=2), "59分钟59秒后"
        )

    def test_round_carries_into_next_unit(self):
        self.assertEqual(self.zh.format(59 * MIN + 59, precision=1, mode="round"), "1小时后")
        self.assertEqual(self.zh.format(23 * HOUR + 59 * MIN, precision=1, mode="round"), "1天后")
        self.assertEqual(self.zh.format(6 * DAY + 12 * HOUR, precision=1, mode="round"), "1周后")
        self.assertEqual(self.zh.format(11 * MONTH + 29 * DAY, precision=1, mode="round"), "1年后")

    def test_round_half_boundary(self):
        # 30s is still sub-minute: most significant unit is the second
        self.assertEqual(self.zh.format(30, precision=1, mode="round"), "30秒后")
        self.assertEqual(self.zh.format(29, precision=1, mode="round"), "29秒后")
        # exactly half of the kept unit (minute) rounds up
        self.assertEqual(self.zh.format(90, precision=1, mode="round"), "2分钟后")
        self.assertEqual(self.zh.format(89, precision=1, mode="round"), "1分钟后")
        # fractional remainder past half of the last kept unit
        self.assertEqual(self.zh.format(59 * MIN + 59.6, precision=2, mode="round"), "1小时后")
        self.assertEqual(self.zh.format(59 * MIN + 59.4, precision=2, mode="round"), "59分钟59秒后")

    def test_round_cascades_multiple_levels(self):
        # 59.6s rounds to 1m; 59m60s would cascade to 1h
        self.assertEqual(self.zh.format(59 * MIN + 59.6, precision=1, mode="round"), "1小时后")
        # 364 days 23 hours rounds into a year boundary cleanly
        self.assertEqual(self.zh.format(YEAR - 1, precision=1, mode="round"), "1年后")

    def test_round_does_not_carry_when_below_half(self):
        self.assertEqual(self.zh.format(61 * MIN + 29, precision=1, mode="round"), "1小时后")
        self.assertEqual(self.en.format(89, precision=1, mode="round"), "in 1 minute")
        self.assertEqual(self.en.format(91, precision=1, mode="round"), "in 2 minutes")

    def test_components_stay_in_canonical_range(self):
        """For any input, every non-leading component must be strictly
        below the next-larger unit (no 60 minutes, no 24 hours, ever)."""
        fmt = self.zh
        samples = list(range(0, 90061))
        rng = random.Random(42)
        samples += [rng.randrange(0, 10**12) for _ in range(3000)]
        for s in samples:
            for precision in (1, 2, 3, 7):
                for mode in ("truncate", "round"):
                    comps = fmt.components(s, precision=precision, mode=mode)
                    self.assertLessEqual(len(comps), precision)
                    for unit, value in comps:
                        self.assertGreater(value, 0)
                    for (upper, _), (lower, lower_value) in zip(comps, comps[1:]):
                        self.assertLess(
                            lower_value * lower.seconds,
                            upper.seconds,
                            f"s={s} precision={precision} mode={mode}: "
                            f"{lower_value} {lower.id} overflows into {upper.id}",
                        )


class TestCrossingAndHugeSpans(unittest.TestCase):
    def setUp(self):
        self.zh = make_zh()
        self.en = make_en()

    def test_cross_day(self):
        self.assertEqual(self.zh.format(DAY + HOUR, precision=2), "1天1小时后")
        self.assertEqual(self.zh.format(2 * DAY - 1, precision=2), "1天23小时后")

    def test_cross_month(self):
        self.assertEqual(self.zh.format(MONTH + 2 * DAY, precision=2), "1个月2天后")
        self.assertEqual(self.zh.format(YEAR + MONTH, precision=2), "1年1个月后")

    def test_huge_span(self):
        years = 10**12 // YEAR
        self.assertEqual(self.zh.format(10**12, precision=1), f"{years}年后")
        self.assertEqual(self.en.format(-10**12, precision=1), f"{years} years ago")

    def test_negative_direction_boundaries(self):
        self.assertEqual(self.zh.format(-(59 * MIN + 59), precision=1), "59分钟前")
        self.assertEqual(
            self.zh.format(-(59 * MIN + 59), precision=1, mode="round"), "1小时前"
        )


class TestArgumentValidation(unittest.TestCase):
    def setUp(self):
        self.zh = make_zh()

    def test_bad_precision(self):
        for bad in (0, -1, 1.5, "2"):
            with self.assertRaises(ValueError):
                self.zh.format(100, precision=bad)

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            self.zh.format(100, mode="ceil")

    def test_non_finite_seconds(self):
        for bad in (float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.zh.format(bad)


class TestLocaleConfigErrors(unittest.TestCase):
    def base_config(self):
        return {
            "language": "test",
            "plural_rule": "one_other",
            "component_template": "{value} {unit}",
            "past": "{span} ago",
            "future": "in {span}",
            "below_min_unit": {"mode": "just_now", "text": "just now"},
            "units": [
                {"id": "minute", "seconds": 60,
                 "forms": {"one": "minute", "other": "minutes"}},
                {"id": "second", "seconds": 1,
                 "forms": {"one": "second", "other": "seconds"}},
            ],
        }

    def assert_config_error(self, config, *needles):
        with self.assertRaises(LocaleConfigError) as ctx:
            Locale(config, source="test://bad")
        message = str(ctx.exception)
        self.assertIn("test://bad", message)
        for needle in needles:
            self.assertIn(needle, message)

    def test_missing_units(self):
        config = self.base_config()
        del config["units"]
        self.assert_config_error(config, "units")

    def test_empty_units(self):
        config = self.base_config()
        config["units"] = []
        self.assert_config_error(config, "units")

    def test_unit_missing_seconds(self):
        config = self.base_config()
        del config["units"][0]["seconds"]
        self.assert_config_error(config, "units[0].seconds", "minute")

    def test_unit_missing_id(self):
        config = self.base_config()
        del config["units"][1]["id"]
        self.assert_config_error(config, "units[1].id")

    def test_reversed_hierarchy_reports_position(self):
        config = self.base_config()
        config["units"] = [config["units"][1], config["units"][0]]
        self.assert_config_error(
            config, "units[1]", "'second'", "'minute'", "largest to smallest"
        )

    def test_duplicate_magnitude_reports_position(self):
        config = self.base_config()
        config["units"][1]["seconds"] = 60
        self.assert_config_error(config, "units[1]", "largest to smallest")

    def test_missing_plural_form(self):
        config = self.base_config()
        config["units"][0]["forms"] = {"other": "minutes"}
        self.assert_config_error(config, "units[0].forms", "'one'", "one_other")

    def test_unknown_plural_rule(self):
        config = self.base_config()
        config["plural_rule"] = "welsh_triple"
        self.assert_config_error(config, "plural_rule", "welsh_triple")

    def test_bad_below_min_mode(self):
        config = self.base_config()
        config["below_min_unit"] = {"mode": "shrug"}
        self.assert_config_error(config, "below_min_unit.mode", "shrug")

    def test_just_now_requires_text(self):
        config = self.base_config()
        config["below_min_unit"] = {"mode": "just_now"}
        self.assert_config_error(config, "below_min_unit.text")

    def test_template_placeholders(self):
        config = self.base_config()
        config["component_template"] = "{value}"
        self.assert_config_error(config, "component_template", "{unit}")
        config = self.base_config()
        config["past"] = "before"
        self.assert_config_error(config, "past", "{span}")

    def test_load_locale_invalid_json(self, ):
        bad = LOCALES / "_broken.json"
        bad.write_text("{not json", encoding="utf-8")
        try:
            with self.assertRaises(LocaleConfigError) as ctx:
                load_locale(bad)
            self.assertIn("_broken.json", str(ctx.exception))
        finally:
            bad.unlink()

    def test_bundled_locales_load(self):
        for name in ("zh.json", "en.json"):
            locale = load_locale(LOCALES / name)
            self.assertEqual(len(locale.units), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
