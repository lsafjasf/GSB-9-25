"""Self-tests for unitlib.  Run: python3 test_unitlib.py  (or -m unittest -v)"""

import unittest
from decimal import Decimal

from unitlib import (
    Q,
    ConversionResult,
    DimensionMismatchError,
    RoundingSpecError,
    UnitSyntaxError,
    UnknownPrefixError,
    UnknownUnitError,
)


class TestBasicAndPrefix(unittest.TestCase):
    def test_prefix_scaling(self):
        self.assertEqual(Q(1, "km"), Q(1000, "m"))
        self.assertEqual(Q(2.5, "cm"), Q(0.025, "m"))
        self.assertEqual(Q(500, "mg"), Q(0.5, "g"))
        self.assertEqual(Q(3, "ms"), Q(0.003, "s"))

    def test_compound_units(self):
        self.assertEqual(Q(36, "km/h"), Q(10, "m/s"))
        self.assertEqual(Q(1, "m^2"), Q(10000, "cm^2"))
        self.assertEqual(Q(1, "ha"), Q(10000, "m^2"))
        self.assertEqual(Q(1, "bar"), Q(100, "kPa"))
        self.assertEqual(Q(1, "bar"), Q(100000, "Pa"))
        self.assertEqual(Q(9.8, "m/s^2"), Q(980, "cm/s^2"))

    def test_power_notations(self):
        self.assertEqual(Q(1, "m2"), Q(1, "m^2"))
        self.assertEqual(Q(1, "m/s2"), Q(1, "m/s^2"))
        self.assertEqual(Q(1, "N"), Q(1, "kg*m/s^2"))


class TestDimensionAlgebra(unittest.TestCase):
    def test_multiply_divide_compose(self):
        self.assertEqual(Q(6, "N") * Q(2, "m") / Q(3, "m"), Q(4, "N"))
        power = Q(10, "N") * Q(2, "m") / Q(4, "s")
        self.assertEqual(power, Q(5, "W"))
        self.assertEqual(power.dims, Q(1, "J/s").dims)
        self.assertEqual(Q(2, "J") / Q(2, "N"), Q(1, "m"))

    def test_add_sub_same_dimension(self):
        self.assertEqual(Q(1, "km") + Q(500, "m"), Q(1.5, "km"))
        self.assertEqual(Q(2, "h") - Q(30, "min"), Q(1.5, "h"))

    def test_add_mismatch_reports_both_dims(self):
        with self.assertRaises(DimensionMismatchError) as ctx:
            Q(1, "m") + Q(1, "s")
        msg = str(ctx.exception)
        self.assertIn("[L]", msg)
        self.assertIn("[T]", msg)

    def test_compare_mismatch_reports_both_dims(self):
        with self.assertRaises(DimensionMismatchError) as ctx:
            Q(1, "kg") < Q(1, "m")
        msg = str(ctx.exception)
        self.assertIn("[M]", msg)
        self.assertIn("[L]", msg)
        with self.assertRaises(DimensionMismatchError):
            Q(1, "Pa") == Q(1, "J")

    def test_scalar_ops(self):
        self.assertEqual(2 * Q(3, "m"), Q(6, "m"))
        self.assertEqual(Q(6, "m") / 2, Q(3, "m"))


class TestErrors(unittest.TestCase):
    def test_unknown_unit(self):
        with self.assertRaises(UnknownUnitError):
            Q(1, "xyz")

    def test_unknown_prefix(self):
        with self.assertRaises(UnknownPrefixError):
            Q(1, "kxyz")

    def test_syntax_error(self):
        for bad in ("m//s", "m**2", "m/", "*m"):
            with self.assertRaises(UnitSyntaxError, msg=bad):
                Q(1, bad)

    def test_conversion_dimension_mismatch(self):
        with self.assertRaises(DimensionMismatchError) as ctx:
            Q(1, "m").to("s", decimals=2)
        self.assertIn("[L]", str(ctx.exception))
        self.assertIn("[T]", str(ctx.exception))
        with self.assertRaises(DimensionMismatchError):
            Q(1, "km/h").to("kg", sig_figs=3)

    def test_error_types_are_distinguishable(self):
        self.assertFalse(issubclass(UnknownPrefixError, UnknownUnitError))
        self.assertFalse(issubclass(UnknownUnitError, UnknownPrefixError))
        self.assertFalse(issubclass(DimensionMismatchError, UnknownUnitError))

    def test_rounding_spec_required(self):
        with self.assertRaises(RoundingSpecError):
            Q(1, "m").to("cm")
        with self.assertRaises(RoundingSpecError):
            Q(1, "m").to("cm", sig_figs=3, decimals=2)
        with self.assertRaises(RoundingSpecError):
            Q(1, "m").to("cm", sig_figs=3, rounding="ROUND_MAGIC")


class TestConversionAndRounding(unittest.TestCase):
    def test_sig_figs(self):
        r = Q(123.456, "km/h").to("m/s", sig_figs=4)
        self.assertIsInstance(r, ConversionResult)
        self.assertEqual(r.value, Decimal("34.29"))
        self.assertIn("4 significant figures", r.rounding)
        self.assertIn("ROUND_HALF_EVEN", r.rounding)

    def test_decimals(self):
        r = Q(1, "m").to("cm", decimals=1)
        self.assertEqual(r.value, Decimal("100.0"))

    def test_rounding_modes_explicit(self):
        up = Q(1.005, "m").to("m", decimals=2, rounding="ROUND_HALF_UP")
        even = Q(1.005, "m").to("m", decimals=2)
        self.assertEqual(up.value, Decimal("1.01"))
        self.assertEqual(even.value, Decimal("1.00"))

    def test_no_implicit_scaling_on_mismatch(self):
        ratio = Q(5, "m") / Q(500, "cm")
        with self.assertRaises(DimensionMismatchError):
            ratio.to("m", decimals=2)


class TestRoundTrip(unittest.TestCase):
    CASES = [
        (123.456, "km/h", "m/s", 8),
        (0.000123, "Pa", "bar", 6),
        (9.99e9, "W", "kW", 10),
        (-45.6, "m/s^2", "cm/s^2", 7),
        (2.5, "kWh", "J", 9),
        (1e-12, "m", "pm", 6),
    ]

    def test_round_trip(self):
        for value, src, dst, sig in self.CASES:
            with self.subTest(value=value, src=src, dst=dst):
                original = Q(value, src)
                forth = original.to(dst, sig_figs=sig)
                back = forth.quantity.to(src, sig_figs=sig)
                err = abs(float(back.value) - value) / abs(value)
                self.assertLess(err, 10 ** (-(sig - 3)),
                                f"round-trip error {err:g} for {value} {src}")

    def test_round_trip_zero(self):
        forth = Q(0, "m").to("cm", decimals=3)
        self.assertEqual(forth.value, Decimal("0"))
        back = forth.quantity.to("m", decimals=3)
        self.assertEqual(float(back.value), 0.0)


class TestEdgeValues(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(Q(0, "m") + Q(0, "cm"), Q(0, "km"))
        self.assertEqual(Q(0, "m") * Q(5, "s"), Q(0, "m*s"))

    def test_negative(self):
        self.assertEqual(Q(-5, "m") + Q(3, "m"), Q(-2, "m"))
        self.assertTrue(Q(-5, "m") < Q(3, "m"))
        self.assertEqual(Q(-2, "m") * Q(-3, "m"), Q(6, "m^2"))

    def test_extreme_magnitudes(self):
        big = Q(1e30, "m").to("km", sig_figs=5)
        self.assertAlmostEqual(float(big.value), 1e27, delta=1e27 * 1e-12)
        tiny = Q(1e-30, "m").to("nm", sig_figs=3)
        self.assertAlmostEqual(float(tiny.value), 1e-21, delta=1e-33)

    def test_dimensionless_ratio(self):
        ratio = Q(5, "m") / Q(500, "cm")
        self.assertTrue(ratio.is_dimensionless())
        self.assertEqual(ratio, Q(1))
        pct = ratio.to("%", decimals=1)
        self.assertEqual(pct.value, Decimal("100.0"))
        ppm = Q(3, "um") / Q(1, "m")
        self.assertEqual(ppm.to("ppm", decimals=0).value, Decimal("3"))

    def test_same_quantity_different_compositions(self):
        self.assertEqual(Q(1, "kWh"), Q(3.6, "MJ"))
        self.assertEqual(Q(1, "N*m"), Q(1, "J"))
        self.assertEqual(Q(1, "L"), Q(1000, "cm^3"))
        self.assertEqual(Q(1, "ha"), Q(0.01, "km^2"))
        self.assertEqual(Q(1, "W*h"), Q(3600, "J"))
        self.assertEqual(Q(1, "kg*m^2/s^2"), Q(1, "J"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
