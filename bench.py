"""Timing comparison: legacy monolith vs staged refactor.

Builds a mixed workload (valid + malformed expressions), parses it
REPEAT times with each implementation, and reports totals.  A ratio near
1.0 means the split into stages costs nothing measurable.
"""

import random
import time

import legacy_parser
from filter_parser import parse as staged_parse

from tests.corpus import CASES, gen_mutation, gen_soup, gen_valid

REPEAT = 30


def build_workload():
    rng = random.Random(42)
    texts = [text for _, text in CASES]
    texts += [gen_valid(rng) for _ in range(600)]
    texts += [gen_soup(rng) for _ in range(200)]
    texts += [gen_mutation(rng) for _ in range(200)]
    return texts


def time_it(fn, workload):
    best = None
    for _ in range(REPEAT):
        start = time.perf_counter()
        for text in workload:
            fn(text)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    return best


def main():
    workload = build_workload()
    print("workload: %d inputs, %d repeats (best-of reported)"
          % (len(workload), REPEAT))

    legacy = time_it(legacy_parser.parse, workload)
    staged = time_it(staged_parse, workload)

    print("legacy  (monolith): %8.3f ms  (%7.2f us/input)"
          % (legacy * 1e3, legacy / len(workload) * 1e6))
    print("staged  (refactor): %8.3f ms  (%7.2f us/input)"
          % (staged * 1e3, staged / len(workload) * 1e6))
    print("ratio staged/legacy: %.3f" % (staged / legacy))


if __name__ == "__main__":
    main()
