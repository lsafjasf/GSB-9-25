"""Command line interface: check / validate / bench."""

import argparse
import json
import random
import sys
import time

from .engine import evaluate
from .ruleset import RuleSet, RuleSetError


def _load_ruleset(path):
    try:
        return RuleSet.load(path)
    except RuleSetError as exc:
        print("rule set error: %s" % exc, file=sys.stderr)
        sys.exit(2)
    except (OSError, json.JSONDecodeError) as exc:
        print("cannot load rule set %s: %s" % (path, exc), file=sys.stderr)
        sys.exit(2)


def cmd_check(args):
    ruleset = _load_ruleset(args.rules)
    with open(args.request, "r", encoding="utf-8") as fh:
        request = json.load(fh)
    decision = evaluate(ruleset, request, use_index=not args.no_index)
    payload = decision.to_dict()
    if args.compact:
        payload = {
            "decision": payload["decision"],
            "decided_by": payload["decided_by"],
            "default_policy_applied": payload["default_policy_applied"],
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return {"allow": 0, "deny": 1}.get(decision.verdict, 2)


def cmd_validate(args):
    ruleset = _load_ruleset(args.rules)
    indexed = sum(len(v) for b in ruleset._buckets.values() for v in b.values())
    print(
        json.dumps(
            {
                "valid": True,
                "rules": len(ruleset.rules),
                "indexed_rules": indexed,
                "unindexed_rules": len(ruleset._always),
                "on_attribute_error": ruleset.on_attribute_error,
                "default_effect": ruleset.default_effect,
            },
            indent=2,
        )
    )
    return 0


def _gen_ruleset(n, seed):
    rng = random.Random(seed)
    n_actions = max(1, n // 25)
    rules = []
    for i in range(n):
        action = "action_%d" % (i % n_actions)
        rtype = "type_%d" % rng.randint(0, 49)
        clearance = rng.randint(0, 4)
        # Effect derived from the conditions: identical conditions can never
        # produce contradictory effects, so generated sets always load.
        effect = "deny" if hash((action, rtype, clearance)) % 7 == 0 else "allow"
        rules.append(
            {
                "id": "r%d" % i,
                "effect": effect,
                "priority": rng.randint(0, 99),
                "when": {
                    "action": {"eq": action},
                    "resource": {"type": {"eq": rtype}},
                    "subject": {"clearance": {"gte": clearance}},
                },
            }
        )
    return {"config": {"on_attribute_error": "error", "default_effect": "deny"},
            "rules": rules}


def _gen_requests(n, n_rules, seed):
    rng = random.Random(seed + 1)
    n_actions = max(1, n_rules // 25)
    reqs = []
    for _ in range(n):
        reqs.append(
            {
                "subject": {"id": "u%d" % rng.randint(0, 999),
                            "clearance": rng.randint(0, 5)},
                "resource": {"type": "type_%d" % rng.randint(0, 49)},
                "action": "action_%d" % rng.randint(0, n_actions - 1),
                "env": {},
            }
        )
    return reqs


def _percentile(sorted_vals, pct):
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * pct))
    return sorted_vals[idx]


def cmd_bench(args):
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    print("%-8s %-12s %-12s %-12s %-12s %-12s" %
          ("rules", "cand/req", "avg_us", "p50_us", "p99_us", "linear_avg_us"))
    for size in sizes:
        ruleset = RuleSet.from_dict(_gen_ruleset(size, args.seed))
        requests = _gen_requests(args.requests, size, args.seed)
        evaluate(ruleset, requests[0])  # warm up
        samples = []
        for req in requests:
            start = time.perf_counter()
            evaluate(ruleset, req)
            samples.append((time.perf_counter() - start) * 1e6)
        samples.sort()
        avg = sum(samples) / len(samples)
        cand = sum(
            evaluate(ruleset, r).explanation["stats"]["candidates"]
            for r in requests[:100]
        ) / min(100, len(requests))
        linear_samples = []
        for req in requests[: max(1, args.requests // 10)]:
            start = time.perf_counter()
            evaluate(ruleset, req, use_index=False)
            linear_samples.append((time.perf_counter() - start) * 1e6)
        linear_avg = sum(linear_samples) / len(linear_samples)
        print("%-8d %-12.1f %-12.2f %-12.2f %-12.2f %-12.2f" % (
            size, cand, avg, _percentile(samples, 0.5),
            _percentile(samples, 0.99), linear_avg))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="abac",
        description="Attribute-based access control with explainable decisions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="evaluate one request")
    p_check.add_argument("--rules", required=True, help="rule set JSON file")
    p_check.add_argument("--request", required=True, help="request JSON file")
    p_check.add_argument("--compact", action="store_true",
                         help="print only decision/decided_by")
    p_check.add_argument("--no-index", action="store_true",
                         help="disable the rule index (linear scan)")
    p_check.set_defaults(func=cmd_check)

    p_val = sub.add_parser("validate", help="validate a rule set and exit")
    p_val.add_argument("--rules", required=True)
    p_val.set_defaults(func=cmd_validate)

    p_bench = sub.add_parser("bench", help="measure decision latency vs rule count")
    p_bench.add_argument("--sizes", default="1000,10000,50000",
                         help="comma-separated rule counts")
    p_bench.add_argument("--requests", type=int, default=2000)
    p_bench.add_argument("--seed", type=int, default=42)
    p_bench.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
