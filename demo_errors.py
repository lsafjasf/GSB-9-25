"""量纲/单位错误用例演示：python3 demo_errors.py"""

from unitlib import (
    ConversionTargetMismatchError,
    DimensionMismatchError,
    Quantity,
    UnknownPrefixError,
    UnknownUnitError,
    convert,
)

cases = [
    ("量纲不一致相加", lambda: Quantity(1, "m") + Quantity(1, "s"),
     DimensionMismatchError),
    ("量纲不一致比较", lambda: Quantity(1, "kg") < Quantity(1, "m"),
     DimensionMismatchError),
    ("换算目标量纲不匹配", lambda: convert(1, "m", "s", sig_figs=3),
     ConversionTargetMismatchError),
    ("未知单位", lambda: convert(1, "blorp", "m", sig_figs=3),
     UnknownUnitError),
    ("未知前缀", lambda: convert(1, "qm", "m", sig_figs=3),
     UnknownPrefixError),
]

for title, fn, expected in cases:
    try:
        fn()
    except expected as exc:
        print("[%s] %s: %s" % (title, type(exc).__name__, exc))
