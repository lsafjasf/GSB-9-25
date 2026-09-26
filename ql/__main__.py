"""Command line entry point: echo the parse result for a query.

Usage:
    python3 -m ql 'SELECT a FROM t WHERE a = 1'
    echo 'SELECT a FROM t' | python3 -m ql
"""

import json
import sys

from .api import parse_query


def main(argv):
    if len(argv) > 1:
        source = " ".join(argv[1:])
    else:
        source = sys.stdin.read()
    print(json.dumps(parse_query(source), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
