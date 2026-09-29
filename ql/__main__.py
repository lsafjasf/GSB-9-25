"""Command line entry point: echo the parse result for a query.

Usage:
    python3 -m ql 'SELECT a FROM t WHERE a = 1'
    python3 -m ql --recover 'SELECT a FROM t WHERE a = 1 EXTRA'
    echo 'SELECT a FROM t' | python3 -m ql
"""

import json
import sys

from .api import parse_query


def main(argv):
    args = list(argv[1:])
    mode = "strict"
    if args and args[0] in ("--recover", "-r"):
        mode = "recover"
        args = args[1:]
    elif args and args[0] == "--strict":
        args = args[1:]
    if args:
        source = " ".join(args)
    else:
        source = sys.stdin.read()
    print(json.dumps(parse_query(source, mode=mode), ensure_ascii=False,
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
