#!/usr/bin/env python3
"""Regenerate README.md from README.template.md with computed numbers.

Every statistic quoted in the README (test counts, corpus sizes, fuzz
case counts, benchmark workload and timings) is computed here from the
actual source tree, so the document cannot drift away from the code.

Usage: python3 scripts/update_readme.py
"""

import ast
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bench
from tests import corpus, test_differential

TEMPLATE = ROOT / "README.template.md"
OUTPUT = ROOT / "README.md"


def count_stage_tests():
    """Number of test_* methods in tests/test_stages.py."""
    tree = ast.parse(
        (ROOT / "tests" / "test_stages.py").read_text(encoding="utf-8")
    )
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def fuzz_generator_names():
    """Fuzz generators exported by tests/corpus.py (gen_* callables)."""
    return sorted(
        name
        for name, value in vars(corpus).items()
        if name.startswith("gen_") and callable(value)
    )


def collect_stats():
    workload = bench.build_workload()
    legacy_s = bench.time_it(bench.legacy_parser.parse, workload)
    staged_s = bench.time_it(bench.staged_parse, workload)

    legacy_ms = legacy_s * 1e3
    staged_ms = staged_s * 1e3
    legacy_us = legacy_s / len(workload) * 1e6
    staged_us = staged_s / len(workload) * 1e6
    ratio = staged_s / legacy_s

    fuzz_gens = fuzz_generator_names()
    per_gen = test_differential.FUZZ_CASES

    return {
        "PY_VERSION": platform.python_version(),
        "STAGES_COUNT": str(count_stage_tests()),
        "CORPUS_NORMAL": str(len(corpus.NORMAL)),
        "CORPUS_BOUNDARY": str(len(corpus.BOUNDARY)),
        "CORPUS_MALFORMED": str(len(corpus.MALFORMED)),
        "CORPUS_TOTAL": str(len(corpus.CASES)),
        "FUZZ_GEN_COUNT": str(len(fuzz_gens)),
        "FUZZ_PER_GEN": str(per_gen),
        "FUZZ_TOTAL": str(per_gen * len(fuzz_gens)),
        "WORKLOAD_SIZE": str(len(workload)),
        "BENCH_REPEAT": str(bench.REPEAT),
        "LEGACY_MS": "%.1f" % legacy_ms,
        "LEGACY_US": "%.1f" % legacy_us,
        "STAGED_MS": "%.1f" % staged_ms,
        "STAGED_US": "%.1f" % staged_us,
        "RATIO": "%.2f" % ratio,
        "SLOWDOWN_PCT": "%d" % round((ratio - 1) * 100),
        "DIFF_US": "%.1f" % (staged_us - legacy_us),
    }


def main():
    stats = collect_stats()
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in stats.items():
        text = text.replace("{{%s}}" % key, value)
    leftovers = [token for token in text.split() if "{{" in token]
    if leftovers:
        raise SystemExit("unresolved placeholders: %s" % leftovers)
    OUTPUT.write_text(text, encoding="utf-8")
    print("wrote %s" % OUTPUT)
    for key in sorted(stats):
        print("  %-14s = %s" % (key, stats[key]))


if __name__ == "__main__":
    main()
