"""Scale & timing benchmark for cursor_pagination.

Run:  python3 benchmark.py
"""

import random
import statistics
import sys
import time

from cursor_pagination import ASC, DESC, Paginator, SortSpec, Store

SECRET = "benchmark-secret"


def build_store(n, seed=7):
    rng = random.Random(seed)
    store = Store(tombstone_capacity=20000)
    for i in range(n):
        store.insert(
            {
                "id": i,
                # heavy duplication on the first two sort keys
                "grp": i % 50,
                "score": rng.randint(0, 500),
                # ~10% NULLs in a sort column
                "rank": None if rng.random() < 0.1 else rng.randint(0, 10_000),
            }
        )
    return store


def drain(paginator, mutate_every=0, store=None, rng=None):
    """Walk all pages; return (count, per_page_times, pages)."""
    times = []
    pages = 0
    count = 0
    page = paginator.first_page()
    while page is not None:
        count += len(page.items)
        pages += 1
        if mutate_every and pages % mutate_every == 0:
            for _ in range(20):
                rid = rng.randrange(0, 2_000_000)
                if rng.random() < 0.5:
                    store.delete(rid)
                else:
                    try:
                        store.insert(
                            {"id": ("new", rng.randrange(1 << 30)),
                             "grp": rng.randrange(50), "score": rng.randrange(500),
                             "rank": rng.randrange(10_000)}
                        )
                    except ValueError:
                        pass
        t0 = time.perf_counter()
        nxt = paginator.next_page(page)
        times.append(time.perf_counter() - t0)
        page = nxt
    return count, times, pages


def fmt(ms):
    return "%.3f ms" % (ms * 1000)


def main():
    spec = SortSpec([("grp", ASC), ("score", DESC), ("rank", ASC)])

    for n, page_size in ((10_000, 100), (100_000, 500), (200_000, 1000)):
        store = build_store(n)
        paginator = Paginator(store, spec, page_size=page_size, secret=SECRET)

        t0 = time.perf_counter()
        count, times, pages = drain(paginator)
        total = time.perf_counter() - t0

        deep = times[len(times) * 9 // 10:]  # last 10% of pages
        print("=== %d rows, page_size=%d ===" % (n, page_size))
        print("  rows walked        : %d in %d pages" % (count, pages))
        print("  total walk time    : %s" % fmt(total))
        print("  avg page fetch     : %s" % fmt(statistics.mean(times)))
        print("  p95 page fetch     : %s" % fmt(sorted(times)[int(len(times) * 0.95)]))
        print("  deep-page avg (last 10%%): %s" % fmt(statistics.mean(deep)))
        print()

    # mutation-heavy deep pagination
    n, page_size = 100_000, 500
    store = build_store(n)
    rng = random.Random(99)
    paginator = Paginator(store, spec, page_size=page_size, secret=SECRET)
    t0 = time.perf_counter()
    count, times, pages = drain(paginator, mutate_every=5, store=store, rng=rng)
    total = time.perf_counter() - t0
    print("=== %d rows, page_size=%d, inserts+deletes every 5 pages ===" % (n, page_size))
    print("  rows walked        : %d in %d pages" % (count, pages))
    print("  total walk time    : %s" % fmt(total))
    print("  avg page fetch     : %s" % fmt(statistics.mean(times)))
    print("  max page fetch     : %s (re-sort after mutation)" % fmt(max(times)))


if __name__ == "__main__":
    sys.exit(main())
