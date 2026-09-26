"""Performance check: run the engine with thousands of rules."""

import statistics
import time

from rule_engine import And, Engine, Field, Flag, Or, Rule, Set

RULE_COUNTS = (1000, 2000, 5000)
RUNS = 30


def build_engine(n_rules):
    engine = Engine(conflict_strategy="priority")
    for i in range(n_rules):
        # half the rules match for the benchmark facts, half do not
        threshold = i % 100
        engine.add_rule(Rule(
            f"rule-{i}",
            And(Field("score").ge(threshold),
                Or(Field("tier").eq("gold"), Field("score").lt(0))),
            [Set(f"out_{i % 50}", i), Flag(f"f{i % 20}")],
            priority=i % 7,
        ))
    return engine


def main():
    facts = {"score": 50, "tier": "gold"}
    for n in RULE_COUNTS:
        engine = build_engine(n)
        engine.run(facts)  # warmup
        samples = []
        for _ in range(RUNS):
            start = time.perf_counter()
            engine.run(facts)
            samples.append((time.perf_counter() - start) * 1000)
        print(
            f"{n:>5} rules | {RUNS} runs | "
            f"mean {statistics.mean(samples):8.3f} ms | "
            f"median {statistics.median(samples):8.3f} ms | "
            f"min {min(samples):8.3f} ms | max {max(samples):8.3f} ms"
        )


if __name__ == "__main__":
    main()
