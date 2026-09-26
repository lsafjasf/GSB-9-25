"""Demo: builds a small rule set, runs it, and prints the full trace."""

from rule_engine import And, Engine, Field, Flag, Or, Rule, Set


def build_engine(strategy="priority"):
    engine = Engine(conflict_strategy=strategy)
    engine.add_rule(Rule(
        "vip-discount",
        And(Field("age").ge(18), Field("tier").in_(["gold", "platinum"])),
        [Set("discount", 0.30), Flag("vip")],
        priority=10,
    ))
    engine.add_rule(Rule(
        "loyalty-discount",
        Field("years").ge(5),
        [Set("discount", 0.15), Flag("loyal")],
        priority=5,
    ))
    engine.add_rule(Rule(
        "student-or-senior",
        Or(Field("student").eq(True), Field("age").ge(65)),
        [Flag("special")],
        priority=3,
    ))
    engine.add_rule(Rule(
        "needs-region",
        Field("region").eq("CN"),
        [Flag("domestic")],
    ))
    return engine


def main():
    engine = build_engine()
    facts = {"age": 40, "tier": "gold", "years": 8, "student": False}
    result = engine.run(facts)
    print("=== input facts ===")
    print(facts)
    print("\n=== execution trace (strategy: priority) ===")
    print(result.trace_text())
    print("\n=== final state ===")
    print("values:", result.state.values)
    print("flags:", sorted(result.state.flags))

    print("\n=== same run with strategy: reject ===")
    try:
        build_engine("reject").run(facts)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
