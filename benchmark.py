"""Performance comparison: monolithic baseline vs. staged refactor.

Stdlib-only.  Runs a fixed workload repeatedly, asserts every old/new result
pair is equal, and prints timings for the whole parse call plus per-stage
timings for the new implementation.

Usage:  python3 benchmark.py [iterations]
"""

import gc
import statistics
import sys
import time

from legacy.monolith import parse_query as old_parse
from ql.assembler import assemble
from ql.lexer import tokenize
from ql.parser import parse as parse_tokens
from ql import parse_query as new_parse

WORKLOAD = [
    "SELECT id, name FROM users WHERE id = 1 LIMIT 10",
    'SELECT a, b, c FROM t WHERE a = 1 AND b <> "x" OR c >= 3 LIMIT 100',
    "SELECT a FROM t",
    "SELECT a,b FROM t WHERE ((a = 1) AND (b = 2 OR c = 3)) LIMIT 0",
    'SELECT name FROM users WHERE name = "admin" AND id != 7 LIMIT 50',
    "SELECT a FROM t WHERE a < 10 OR b > 20 AND c = 30",
    "SELECT a FROM t LIMIT 1000",
    "SELECT a, b, c FROM t WHERE (a = 1 OR b = 2) AND (c = 3) LIMIT 5",
    # malformed inputs exercise error rendering too
    "SELECT a, a FROM t",
    "SELECT a FROM t WHERE 1 = 1",
    "SELECT a FROM t WHERE (a = 1",
    "SELECT a FROM t LIMIT 1001",
    'SELECT a FROM t WHERE a = "bad\\q"',
    "SELECT a FROM t @junk",
    "",
    "SELECT FROM",
]


def _check_equivalence():
    for text in WORKLOAD:
        if old_parse(text) != new_parse(text):
            raise AssertionError(f"equivalence violated for {text!r}")


def _time(fn, iterations):
    gc.collect()
    gc.disable()
    try:
        start = time.perf_counter()
        for _ in range(iterations):
            for text in WORKLOAD:
                fn(text)
        elapsed = time.perf_counter() - start
    finally:
        gc.enable()
    return elapsed


def _time_stages(iterations):
    """Return per-stage elapsed seconds (source -> result or error)."""
    totals = {"lexer": 0.0, "parser": 0.0, "assembler": 0.0,
              "errorgen": 0.0}
    gc.collect()
    gc.disable()
    try:
        for _ in range(iterations):
            for text in WORKLOAD:
                parse_error = None
                t0 = time.perf_counter()
                try:
                    tokens = tokenize(text)
                except Exception as exc:  # noqa: BLE001 - timing probe
                    tokens = ()
                    parse_error = exc
                t1 = time.perf_counter()
                program = None
                if parse_error is None:
                    try:
                        program = parse_tokens(tokens, len(text))
                    except Exception as exc:  # noqa: BLE001
                        parse_error = exc
                t2 = time.perf_counter()
                if program is not None:
                    try:
                        assemble(program)
                    except Exception as exc:  # noqa: BLE001
                        parse_error = exc
                t3 = time.perf_counter()
                if parse_error is not None:
                    from ql.errorgen import render
                    render(parse_error, text)
                t4 = time.perf_counter()
                totals["lexer"] += t1 - t0
                totals["parser"] += t2 - t1
                totals["assembler"] += t3 - t2
                totals["errorgen"] += t4 - t3
    finally:
        gc.enable()
    return totals


def main(argv):
    iterations = int(argv[1]) if len(argv) > 1 else 3000
    _check_equivalence()

    runs_old = [_time(old_parse, iterations) for _ in range(5)]
    runs_new = [_time(new_parse, iterations) for _ in range(5)]
    stages = _time_stages(iterations)

    calls = iterations * len(WORKLOAD)
    med_old = statistics.median(runs_old)
    med_new = statistics.median(runs_new)
    ratio = med_new / med_old

    print(f"workload size : {len(WORKLOAD)} queries")
    print(f"iterations    : {iterations} ({calls} parse calls per run)")
    print("equivalence   : all old/new result pairs identical")
    print()
    print(f"monolith  median : {med_old * 1000:9.2f} ms "
          f"({med_old / calls * 1e6:7.3f} us/call)")
    print(f"refactored median: {med_new * 1000:9.2f} ms "
          f"({med_new / calls * 1e6:7.3f} us/call)")
    print(f"ratio new/old    : {ratio:9.3f}x")
    print()
    print("refactored per-stage time (last run):")
    stage_total = sum(stages.values())
    for name, elapsed in stages.items():
        print(f"  {name:<10}: {elapsed * 1000:8.2f} ms "
              f"({elapsed / stage_total * 100:5.1f}%)")

    # Guard the "must not be significantly slower" requirement.
    if ratio > 1.15:
        print("\nFAIL: refactored parser is more than 15% slower",
              file=sys.stderr)
        return 1
    print("\nPASS: within 15% performance budget")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
