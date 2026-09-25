"""End-to-end scenario on virtual time: prints stale markers and stats.

Run:  python3 demo.py
"""

from __future__ import annotations

from swr_cache.cache import BackoffPolicy, OriginError, SWRCache, StaleMarker
from swr_cache.runtime import VirtualRuntime
from tests.helpers import FakeOrigin

TTL = 10.0
GRACE = 30.0
TIMEOUT = 2.0


def show(label: str, fut, clock: VirtualRuntime) -> None:
    res = fut.result(timeout=0)
    if isinstance(res, StaleMarker):
        nxt = f"{res.next_retry_at:.1f}" if res.next_retry_at is not None else "-"
        print(
            f"  t={clock.now():5.1f}  {label:18s} STALE  "
            f"value={res.value!r} version={res.version} "
            f"expired_at={res.expired_at:.1f} stale_for={res.stale_for:.1f}s "
            f"failures={res.failures} next_retry_at={nxt}"
        )
    else:
        print(f"  t={clock.now():5.1f}  {label:18s} FRESH  value={res!r}")


def main() -> None:
    clock = VirtualRuntime()
    origin = FakeOrigin()
    cache = SWRCache(
        origin=origin,
        ttl=TTL,
        grace=GRACE,
        fetch_timeout=TIMEOUT,
        backoff=BackoffPolicy(base_delay=1.0, factor=2.0, max_delay=8.0),
        scheduler=clock,
    )

    print("== 1) first miss: 3 concurrent requests, 1 origin call ==")
    f1, f2, f3 = cache.get("user:1"), cache.get("user:1"), cache.get("user:1")
    origin.resolve({"name": "ada", "rev": 1})
    clock.advance(0)
    show("miss #1", f1, clock)
    show("miss #2 (merged)", f2, clock)
    show("miss #3 (merged)", f3, clock)

    print("== 2) fresh hit ==")
    clock.advance(5)
    show("hit", cache.get("user:1"), clock)

    print("== 3) just expired: stale served, one background refresh starts ==")
    clock.advance(6)  # t=11, 1s past expiry
    show("stale read #1", cache.get("user:1"), clock)
    show("stale read #2", cache.get("user:1"), clock)

    print("== 4) refresh fails twice (backoff 1s, 2s); stale value retained ==")
    origin.fail(OriginError("db down"))
    clock.advance(0)
    show("stale after fail#1", cache.get("user:1"), clock)
    clock.advance(1.0)
    origin.fail(OriginError("db down"))
    clock.advance(0)

    print("== 5) refresh succeeds after 2s backoff; next read sees new value ==")
    clock.advance(2.0)
    origin.resolve({"name": "ada", "rev": 2})
    clock.advance(0)
    show("read after refresh", cache.get("user:1"), clock)
    clock.advance(1)
    show("fresh hit", cache.get("user:1"), clock)

    print()
    snap = cache.stats_snapshot()
    print("== stats ==")
    for key, val in snap.items():
        print(f"  {key:18s} = {val}")
    print()
    print(
        "  identity: requests == hits + stale_hits + coalesced + blocking_loads"
        f"  -> {snap['requests']} == "
        f"{snap['hits']} + {snap['stale_hits']} + {snap['coalesced']} + {snap['blocking_loads']}"
    )
    print(f"  backoff series (failures 1..6): {cache.backoff_series(6)}")


if __name__ == "__main__":
    main()
