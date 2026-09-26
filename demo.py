"""Demo: prints a full execution trace and a performance benchmark.

Run: python3 demo.py
"""

import json
import time

from rule_engine import ConflictError, Rule, RuleEngine


def sample_trace():
    engine = RuleEngine(conflict_strategy="priority")
    engine.add_rule(Rule(
        "adult", {"field": "age", "op": ">=", "value": 18},
        [{"set": "category", "value": "adult"}], priority=1))
    engine.add_rule(Rule(
        "vip_member",
        {"and": [{"field": "tier", "op": "in", "value": ["gold", "platinum"]},
                 {"field": "active", "op": "==", "value": True}]},
        [{"set": "discount", "value": 0.2}, {"flag": "vip"}], priority=10))
    engine.add_rule(Rule(
        "student_discount",
        {"and": [{"field": "age", "op": "<", "value": 25},
                 {"not": {"field": "tier", "op": "==", "value": "platinum"}}]},
        [{"set": "discount", "value": 0.1}], priority=5))
    engine.add_rule(Rule(
        "needs_review", {"field": "risk_score", "op": ">", "value": 80},
        [{"flag": "manual_review"}], priority=8))  # field missing -> skipped

    facts = {"age": 20, "tier": "gold", "active": True}
    result = engine.run(facts)
    print("=== facts ===")
    print(json.dumps(facts, ensure_ascii=False))
    print("\n=== trace (strategy=priority) ===")
    print(json.dumps(result.trace, indent=2, ensure_ascii=False))
    print("\n=== final state ===")
    print(json.dumps(result.state, ensure_ascii=False),
          "flags:", sorted(result.flags))

    reject_engine = RuleEngine(conflict_strategy="reject")
    reject_engine.add_rule(Rule(
        "a", {"field": "x", "op": "==", "value": 1},
        [{"set": "v", "value": "A"}], priority=2))
    reject_engine.add_rule(Rule(
        "b", {"field": "x", "op": "==", "value": 1},
        [{"set": "v", "value": "B"}], priority=1))
    print("\n=== reject strategy on conflict ===")
    try:
        reject_engine.run({"x": 1})
    except ConflictError as exc:
        print("ConflictError:", exc)
        print("recorded conflicts:",
              json.dumps(exc.conflicts, ensure_ascii=False))


def benchmark(rule_counts=(1000, 2000, 5000), repeat=20):
    print("\n=== benchmark (avg of %d runs) ===" % repeat)
    for count in rule_counts:
        engine = RuleEngine(conflict_strategy="priority")
        for i in range(count):
            if i % 3 == 0:
                cond = {"and": [{"field": "n", "op": ">=", "value": i},
                                {"field": "kind", "op": "in", "value": ["a", "b"]}]}
            elif i % 3 == 1:
                cond = {"or": [{"field": "n", "op": "<", "value": i},
                               {"field": "kind", "op": "==", "value": "z"}]}
            else:
                cond = {"not": {"field": "n", "op": "==", "value": i}}
            engine.add_rule(Rule("rule-%05d" % i, cond,
                                 [{"set": "bucket_%d" % (i % 64), "value": i},
                                  {"flag": "f%d" % i}],
                                 priority=i % 7))
        facts = {"n": 1000, "kind": "a"}
        engine.run(facts)  # warmup
        start = time.perf_counter()
        result = None
        for _ in range(repeat):
            result = engine.run(facts)
        elapsed = (time.perf_counter() - start) / repeat
        print("rules=%5d  matched=%5d  actions=%5d  conflicts=%4d  avg=%.2f ms"
              % (count, len(result.trace["matched"]),
                 len(result.trace["actions"]), len(result.trace["conflicts"]),
                 elapsed * 1000))


if __name__ == "__main__":
    sample_trace()
    benchmark()
