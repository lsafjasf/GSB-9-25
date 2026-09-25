"""Command line interface: validate / check / bench."""

import argparse
import json
import sys

from .bench import run_bench
from .engine import DEFAULT_EFFECTS, ON_MISSING_POLICIES, Policy
from .errors import AbacError


def _add_policy_args(parser):
    parser.add_argument("--on-missing-attribute", choices=ON_MISSING_POLICIES,
                        default="error",
                        help="policy for missing/ill-typed attributes "
                             "(default: error)")
    parser.add_argument("--default-effect", choices=DEFAULT_EFFECTS,
                        default="deny",
                        help="decision when no rule matches (default: deny)")


def _cmd_validate(args):
    Policy.from_file(args.rules,
                     on_missing_attribute=args.on_missing_attribute,
                     default_effect=args.default_effect)
    print("OK: %s loaded, no conflicts" % args.rules)
    return 0


def _cmd_check(args):
    policy = Policy.from_file(args.rules,
                              on_missing_attribute=args.on_missing_attribute,
                              default_effect=args.default_effect)
    with open(args.request, "r", encoding="utf-8") as fh:
        request = json.load(fh)
    result = policy.decide(request, explain=not args.compact)
    if args.compact:
        result["explanation"].pop("unmatched_rules", None)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return {"allow": 0, "deny": 1, "error": 2}[result["decision"]]


def _cmd_bench(args):
    sizes = [int(s) for s in args.sizes.split(",")]
    rows = run_bench(sizes, args.requests)
    print("%-10s %-10s %-10s %-10s %-10s %-10s"
          % ("rules", "requests", "avg(us)", "p50(us)", "p95(us)", "max(us)"))
    for row in rows:
        print("%-10d %-10d %-10.1f %-10.1f %-10.1f %-10.1f"
              % (row["rules"], row["requests"], row["avg_us"], row["p50_us"],
                 row["p95_us"], row["max_us"]))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="abac", description="Attribute-based access control engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="load rules and check conflicts")
    p_validate.add_argument("--rules", required=True)
    _add_policy_args(p_validate)
    p_validate.set_defaults(func=_cmd_validate)

    p_check = sub.add_parser("check", help="evaluate one request with explanation")
    p_check.add_argument("--rules", required=True)
    p_check.add_argument("--request", required=True)
    p_check.add_argument("--compact", action="store_true",
                         help="omit the unmatched-rule list from the explanation")
    _add_policy_args(p_check)
    p_check.set_defaults(func=_cmd_check)

    p_bench = sub.add_parser("bench", help="measure decision latency vs rule count")
    p_bench.add_argument("--sizes", default="1000,10000,100000",
                         help="comma-separated rule counts")
    p_bench.add_argument("--requests", type=int, default=2000)
    p_bench.set_defaults(func=_cmd_bench)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AbacError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
