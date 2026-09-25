"""Scale benchmark: ~2000 rules, trie lookup vs naive linear scan.

Run: python3 bench.py [num_rules] [num_lookups]
"""

import random
import sys
import time

from router import Router, load_routes


def gen_rules(n: int, seed: int = 42) -> str:
    rng = random.Random(seed)
    lines = []
    methods = ["GET", "POST", "PUT", "DELETE", "-"]
    for i in range(n):
        method = rng.choice(methods)
        host = rng.choice(["-", "-", "-", "api.", "admin."])
        depth = rng.randint(2, 4)
        segs = []
        for d in range(depth):
            r = rng.random()
            if r < 0.75:
                segs.append(f"s{i}_{d}")
            elif r < 0.9:
                segs.append("{p%d}" % d)
            else:
                segs.append("*")
        if rng.random() < 0.1:
            segs.append("**")
        lines.append(f"{method} {host} /" + "/".join(segs))
    # duplicates/shadows would abort a strict load; this generator produces
    # mostly unique shapes, but dedupe defensively via non-strict load
    return "\n".join(lines)


def linear_match(rules, method, host, path):
    """Naive baseline: test every rule in registration order."""
    from router import _matches_scope
    segs = [s for s in path.split("/") if s]
    best = None
    for rule in rules:
        if not _matches_scope(rule, method, host):
            continue
        pat = rule.segments
        tail = pat and pat[-1].kind == 0
        head = pat[:-1] if tail else pat
        if not tail and len(head) != len(segs):
            continue
        if len(segs) < len(head):
            continue
        ok = True
        for ps, rs in zip(head, segs):
            if ps.kind == 3 and ps.text != rs:  # STATIC
                ok = False
                break
        if not ok:
            continue
        key = tuple(s.kind for s in pat)
        if best is None or _kinds_better(key, best[0]):
            best = (key, rule)
    return best[1] if best else None


def _kinds_better(ka, kb):
    """Same total order as the trie: first differing kind decides; if one
    sequence is a prefix of the other, the shorter (more exact) wins."""
    for x, y in zip(ka, kb):
        if x != y:
            return x > y
    return len(ka) < len(kb)


def main() -> None:
    n_rules = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    n_lookups = int(sys.argv[2]) if len(sys.argv) > 2 else 20000

    text = gen_rules(n_rules)
    t0 = time.perf_counter()
    router = load_routes(text, strict=False)
    t1 = time.perf_counter()
    print(f"rules loaded : {len(router.rules)} "
          f"({len(router.load_problems)} conflicts rejected) "
          f"in {(t1 - t0) * 1e3:.1f} ms")

    rng = random.Random(7)
    paths = []
    for _ in range(n_lookups):
        rule = rng.choice(router.rules)
        segs = [f"s{rule.order}_{d}" if s.kind == 3 else f"v{d}"
                for d, s in enumerate(rule.segments) if s.kind != 0]
        if rng.random() < 0.3:  # some misses
            segs.append("nope")
        paths.append("/" + "/".join(segs))
    method, host = "GET", "api.example.com"

    t0 = time.perf_counter()
    hits = 0
    for p in paths:
        if router.match(method, host, p) is not None:
            hits += 1
    t1 = time.perf_counter()
    trie_us = (t1 - t0) / n_lookups * 1e6
    print(f"trie match   : {n_lookups} lookups, {hits} hits, "
          f"{trie_us:.2f} us/lookup ({(t1 - t0) * 1e3:.1f} ms total)")

    sample = paths[:2000]
    t0 = time.perf_counter()
    for p in sample:
        linear_match(router.rules, method, host, p)
    t1 = time.perf_counter()
    lin_us = (t1 - t0) / len(sample) * 1e6
    print(f"linear scan  : {len(sample)} lookups, "
          f"{lin_us:.2f} us/lookup ({(t1 - t0) * 1e3:.1f} ms total)")
    print(f"speedup      : {lin_us / trie_us:.1f}x")


if __name__ == "__main__":
    main()
