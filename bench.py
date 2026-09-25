"""Scale benchmark: segment-trie router vs naive linear scan.

Run: python3 bench.py [n_rules] [n_lookups]
"""
import random
import sys
import time

from router import Router, parse_pattern, _split_path, _norm_method, _norm_host_prefix


def build_router(n, seed=42):
    rng = random.Random(seed)
    router = Router()
    linear = []
    i = 0
    while len(linear) < n:
        method = rng.choice(["GET", "POST", "PUT", "DELETE"])
        host = "tenant%d." % rng.randrange(20)
        depth = rng.randint(2, 4)
        parts = ["/svc%03d" % rng.randrange(200)]
        for _ in range(depth - 1):
            parts.append(rng.choice(["/{id}", "/*", "/v%d" % rng.randrange(5)]))
        pattern = "".join(parts)
        try:
            router.add_rule(pattern, method=method, host_prefix=host)
        except Exception:
            i += 1
            if i > n * 20:
                break
            continue
        linear.append((method, host, parse_pattern(pattern)))
    return router, linear


def linear_match(linear, path, method, host):
    segs = _split_path(path)
    best = None
    best_key = None
    for idx, (m, h, pat) in enumerate(linear):
        if m != method or not host.startswith(h):
            continue
        if pat and pat[-1].kind == 3:
            if len(segs) < len(pat) - 1:
                continue
        elif len(segs) != len(pat):
            continue
        ok = True
        for k, seg in enumerate(pat):
            if seg.kind == 0 and (k >= len(segs) or segs[k] != seg.literal):
                ok = False
                break
            if seg.kind in (1, 2) and k >= len(segs):
                ok = False
                break
        if not ok:
            continue
        key = tuple(s.kind for s in pat)
        if best_key is None or key < best_key or (key == best_key and idx < best):
            best, best_key = idx, key
    return best


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    lookups = int(sys.argv[2]) if len(sys.argv) > 2 else 20000
    router, linear = build_router(n)
    rng = random.Random(7)
    samples = []
    for _ in range(lookups):
        svc = rng.randrange(200)
        path = "/svc%03d/%s/%s" % (svc, rng.choice(["abc", "x"]), rng.choice(["v1", "v9", "z"]))
        samples.append((path, rng.choice(["GET", "POST", "PUT", "DELETE"]),
                        "tenant%d.example.com" % rng.randrange(20)))

    t0 = time.perf_counter()
    hits = 0
    for path, method, host in samples:
        if router.match(path, method, host):
            hits += 1
    t1 = time.perf_counter()
    trie_us = (t1 - t0) / lookups * 1e6

    t0 = time.perf_counter()
    for path, method, host in samples[: lookups // 20]:
        linear_match(linear, path, method, host)
    t1 = time.perf_counter()
    lin_us = (t1 - t0) / (lookups // 20) * 1e6

    print("rules loaded        : %d" % len(linear))
    print("lookups             : %d (hits: %d)" % (lookups, hits))
    print("trie match          : %.2f us/lookup" % trie_us)
    print("linear scan baseline: %.2f us/lookup" % lin_us)
    print("speedup             : %.1fx" % (lin_us / trie_us))


if __name__ == "__main__":
    main()
