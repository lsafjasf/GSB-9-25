"""unitlib 自测：python3 -m unittest test_unitlib -v"""

import unittest

from unitlib import (
    ConversionTargetMismatchError,
    DimensionMismatchError,
    Quantity,
    UnknownPrefixError,
    UnknownUnitError,
    convert,
    format_dims,
)


class TestBasicConversion(unittest.TestCase):
    def test_prefix_scaling(self):
        self.assertEqual(convert(1, "km", "m", sig_figs=10), 1000.0)
        self.assertEqual(convert(1, "mg", "kg", sig_figs=10), 1e-6)
        self.assertEqual(convert(1, "µs", "s", sig_figs=10), 1e-6)

    def test_velocity(self):
        self.assertEqual(convert(72, "km/h", "m/s", sig_figs=10), 20.0)

    def test_area(self):
        self.assertEqual(convert(1, "m^2", "cm^2", sig_figs=10), 1e4)
        self.assertEqual(convert(1, "L", "cm^3", sig_figs=10), 1000.0)

    def test_pressure(self):
        self.assertEqual(convert(1, "kPa", "Pa", sig_figs=10), 1000.0)
        self.assertEqual(convert(1, "Pa", "N/m^2", sig_figs=10), 1.0)
        self.assertEqual(convert(1, "bar", "Pa", sig_figs=10), 1e5)

    def test_acceleration(self):
        self.assertAlmostEqual(convert(1, "m/s^2", "cm/s^2", sig_figs=10),
                               100.0)

    def test_derived_named_units(self):
        self.assertEqual(convert(1, "N", "kg*m/s^2", sig_figs=10), 1.0)
        self.assertEqual(convert(1, "J", "N*m", sig_figs=10), 1.0)
        self.assertEqual(convert(1, "kWh", "J", places=0), 3.6e6)
        self.assertEqual(convert(1, "atm", "Pa", sig_figs=10), 101325.0)


class TestDimensionArithmetic(unittest.TestCase):
    def test_add_same_dims_different_units(self):
        q = Quantity(1, "km") + Quantity(500, "m")
        self.assertEqual(q.to("m", sig_figs=10), 1500.0)

    def test_add_mismatch_reports_both_dims(self):
        with self.assertRaises(DimensionMismatchError) as ctx:
            Quantity(1, "m") + Quantity(1, "s")
        msg = str(ctx.exception)
        self.assertIn("L", msg)
        self.assertIn("T", msg)
        self.assertEqual(format_dims(ctx.exception.left_dims), "L")
        self.assertEqual(format_dims(ctx.exception.right_dims), "T")

    def test_compare_mismatch_raises(self):
        with self.assertRaises(DimensionMismatchError) as ctx:
            Quantity(1, "kg") < Quantity(1, "m")
        self.assertIn("M", str(ctx.exception))
        self.assertIn("L", str(ctx.exception))

    def test_multiply_compose_dims(self):
        f = Quantity(2, "kg") * Quantity(3, "m/s^2")
        self.assertEqual(f, Quantity(6, "N"))  # 合成后等于导出单位牛顿

    def test_divide_cancel_dims(self):
        d = Quantity(10, "m") / Quantity(2, "s")
        self.assertEqual(d, Quantity(5, "m/s"))
        length = Quantity(5, "m/s") * Quantity(2, "s")
        self.assertEqual(length, Quantity(10, "m"))  # 时间约分掉

    def test_power(self):
        area = Quantity(3, "m") ** 2
        self.assertEqual(area, Quantity(9, "m^2"))

    def test_pressure_from_composition(self):
        p = Quantity(100, "N") / (Quantity(2, "m") ** 2)
        self.assertEqual(p, Quantity(25, "Pa"))
        self.assertEqual(p, Quantity(25, "kg/m/s^2"))


class TestSameQuantityDifferentForms(unittest.TestCase):
    def test_equal_across_composite_forms(self):
        self.assertEqual(Quantity(1, "N"), Quantity(1, "kg*m/s^2"))
        self.assertEqual(Quantity(1, "J"), Quantity(1, "N*m"))
        self.assertEqual(Quantity(1, "J"), Quantity(1, "kg*m^2/s^2"))
        self.assertEqual(Quantity(1, "Pa"), Quantity(1, "N/m^2"))
        self.assertEqual(Quantity(1, "W"), Quantity(1, "J/s"))

    def test_compare_across_units(self):
        self.assertTrue(Quantity(1, "km") > Quantity(999, "m"))
        self.assertTrue(Quantity(1, "h") == Quantity(60, "min"))


class TestDimensionless(unittest.TestCase):
    def test_ratio_is_dimensionless(self):
        r = Quantity(2, "m") / Quantity(50, "cm")
        self.assertTrue(r.is_dimensionless)
        self.assertEqual(r, 4.0)  # 无量纲量可直接与数值比较

    def test_dimensionless_unit_string(self):
        self.assertEqual(convert(0.5, "1", "1", sig_figs=3), 0.5)


class TestEdgeValues(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(convert(0, "km", "m", sig_figs=5), 0.0)
        self.assertEqual(convert(0, "km", "m", places=3), 0.0)
        self.assertEqual(Quantity(0, "J"), Quantity(0, "N*m"))

    def test_negative(self):
        self.assertEqual(convert(-5, "km", "m", sig_figs=5), -5000.0)
        self.assertTrue(Quantity(-1, "m") < Quantity(0, "m"))

    def test_tiny(self):
        self.assertEqual(convert(1e-30, "m", "fm", sig_figs=10), 1e-15)

    def test_huge(self):
        self.assertEqual(convert(1e30, "m", "Em", sig_figs=10), 1e12)


class TestRounding(unittest.TestCase):
    def test_sig_figs(self):
        self.assertEqual(convert(1.23456, "m", "m", sig_figs=3), 1.23)
        self.assertEqual(convert(123456, "m", "m", sig_figs=3), 123000.0)

    def test_places(self):
        self.assertEqual(convert(1.005, "m", "m", places=2,
                                 rounding="half_up"), 1.01)

    def test_rounding_modes_differ(self):
        # 2.5 保留 1 位有效数字：half_even -> 2，half_up -> 3
        self.assertEqual(convert(2.5, "m", "m", sig_figs=1,
                                 rounding="half_even"), 2.0)
        self.assertEqual(convert(2.5, "m", "m", sig_figs=1,
                                 rounding="half_up"), 3.0)
        self.assertEqual(convert(-2.5, "m", "m", sig_figs=1,
                                 rounding="floor"), -3.0)
        self.assertEqual(convert(-2.5, "m", "m", sig_figs=1,
                                 rounding="ceiling"), -2.0)

    def test_precision_is_mandatory(self):
        with self.assertRaises(ValueError):
            convert(1, "m", "m")  # 未指定精度
        with self.assertRaises(ValueError):
            convert(1, "m", "m", sig_figs=3, places=2)  # 两者同时给


class TestRoundTrip(unittest.TestCase):
    """往返换算：A -> B -> A，结果必须在给定精度内一致。"""

    CASES = [
        (1.23456789, "kPa", "Pa"),
        (72.0, "km/h", "m/s"),
        (3.6, "MJ", "J"),
        (2.5, "m^2", "cm^2"),
        (9.80665, "m/s^2", "cm/s^2"),
        (101325.0, "atm", "bar"),
        (1e-12, "s", "ns"),
        (1e18, "J", "EJ"),
        (-273.15, "K", "K"),
    ]

    def test_roundtrip_unrounded_precision(self):
        for value, u1, u2 in self.CASES:
            with self.subTest(value=value, u1=u1, u2=u2):
                fwd = convert(value, u1, u2, sig_figs=12)
                back = convert(fwd, u2, u1, sig_figs=12)
                rel = abs(back - value) / abs(value) if value else abs(back)
                self.assertLessEqual(rel, 1e-10,
                                     "roundtrip error %g for %s %s"
                                     % (rel, value, u1))

    def test_roundtrip_error_within_stated_precision(self):
        # 显式验证：舍入到 n 位有效数字的往返误差不超过该精度量级
        sig = 6
        for value, u1, u2 in self.CASES:
            if value == 0:
                continue
            with self.subTest(value=value, u1=u1, u2=u2):
                fwd = convert(value, u1, u2, sig_figs=sig)
                back = convert(fwd, u2, u1, sig_figs=sig)
                rel = abs(back - value) / abs(value)
                self.assertLessEqual(rel, 10 ** (-(sig - 2)))


class TestErrors(unittest.TestCase):
    def test_unknown_unit(self):
        with self.assertRaises(UnknownUnitError):
            convert(1, "blorp", "m", sig_figs=3)

    def test_unknown_prefix(self):
        # 'q' 不是已知前缀，但 'm' 是合法单位 -> 可区分的 UnknownPrefixError
        with self.assertRaises(UnknownPrefixError):
            convert(1, "qm", "m", sig_figs=3)

    def test_target_dim_mismatch(self):
        with self.assertRaises(ConversionTargetMismatchError):
            convert(1, "m", "s", sig_figs=3)
        with self.assertRaises(ConversionTargetMismatchError):
            convert(1, "m/s", "m", sig_figs=3)

    def test_errors_are_distinguishable(self):
        errs = []
        for call in (
            lambda: convert(1, "blorp", "m", sig_figs=3),
            lambda: convert(1, "qm", "m", sig_figs=3),
            lambda: convert(1, "m", "s", sig_figs=3),
            lambda: Quantity(1, "m") + Quantity(1, "s"),
        ):
            try:
                call()
            except Exception as exc:  # noqa: BLE001
                errs.append(type(exc))
        self.assertEqual(len(set(errs)), 4)


class TestConfigDriven(unittest.TestCase):
    def test_new_unit_from_config_only(self):
        # 用一个临时配置声明新单位，不改代码即可使用
        import json
        import os
        import tempfile
        from unitlib import UnitRegistry

        cfg = {
            "prefixes": {"k": 1e3},
            "base": {"m": "L", "s": "T"},
            "derived": {"kn": "1852*m/h", "h": "3600*s"},
        }
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            path = fh.name
        try:
            reg = UnitRegistry(json.load(open(path)))
            q = Quantity(1, "kn", registry=reg)
            self.assertAlmostEqual(q.to("m/s", sig_figs=10), 1852 / 3600)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
