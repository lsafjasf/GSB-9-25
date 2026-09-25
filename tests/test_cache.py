"""Timing/concurrency/failure tests for the stale-while-revalidate cache.

Every test runs on virtual time; origin calls are completed manually so that
all interleavings are deterministic.
"""

from __future__ import annotations

import threading
import unittest

from swr_cache.cache import (
    BackoffPolicy,
    OriginError,
    OriginTimeout,
    SWRCache,
    StaleMarker,
)
from swr_cache.runtime import ThreadScheduler, ThreadedOrigin, VirtualRuntime

from .helpers import FakeOrigin, marker_of, value_of

TTL = 10.0
GRACE = 30.0
TIMEOUT = 2.0
BACKOFF = BackoffPolicy(base_delay=1.0, factor=2.0, max_delay=8.0)
# Intervals after failures 1..6: 1, 2, 4, 8, 8, 8 (capped).
EXPECTED_SERIES = [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def make_cache(origin=None, ttl=TTL, grace=GRACE, timeout=TIMEOUT, clock=None):
    clock = clock or VirtualRuntime()
    origin = origin or FakeOrigin()
    cache = SWRCache(
        origin=origin,
        ttl=ttl,
        grace=grace,
        fetch_timeout=timeout,
        backoff=BACKOFF,
        scheduler=clock,
    )
    return cache, origin, clock


class FirstMissTests(unittest.TestCase):
    def test_first_miss_is_served_from_origin(self):
        cache, origin, _clock = make_cache()
        fut = cache.get("k")
        self.assertFalse(fut.done())
        self.assertEqual(origin.call_count, 1)
        origin.resolve("v1")
        self.assertEqual(value_of(fut), "v1")
        self.assertEqual(cache.stats_snapshot()["hits"], 0)

    def test_concurrent_misses_coalesce_to_one_origin_call(self):
        cache, origin, _clock = make_cache()
        waiters = [cache.get("k") for _ in range(5)]
        # Counter assertion: exactly one origin call for five requests.
        self.assertEqual(origin.call_count, 1)
        origin.resolve("v1")
        for fut in waiters:
            self.assertEqual(value_of(fut), "v1")
        snap = cache.stats_snapshot()
        self.assertEqual(snap["blocking_loads"], 1)
        self.assertEqual(snap["coalesced"], 4)
        self.assertEqual(snap["requests"], 5)
        self.assertTrue(snap["identity_holds"])


class ExpiryConcurrencyTests(unittest.TestCase):
    def test_expired_reads_are_stale_and_one_refresh_serves_all(self):
        cache, origin, clock = make_cache()
        f0 = cache.get("k")
        origin.resolve("v1")
        f0.result(timeout=0)

        clock.advance(TTL + 1)
        stale_futs = [cache.get("k") for _ in range(4)]

        # All four reads get stale data immediately; only one refresh started.
        for fut in stale_futs:
            marker = marker_of(fut)
            self.assertEqual(marker.value, "v1")
            self.assertEqual(marker.version, 1)
            self.assertAlmostEqual(marker.stale_for, 1.0)
        self.assertEqual(origin.call_count, 2)  # initial + single refresh

        origin.resolve("v2")
        clock.advance(0)
        self.assertEqual(value_of(cache.get("k")), "v2")

        snap = cache.stats_snapshot()
        self.assertEqual(snap["requests"], 1 + 4 + 1)
        self.assertEqual(snap["stale_hits"], 4)
        self.assertEqual(snap["hits"], 1)
        self.assertEqual(snap["origin_calls"], 2)
        self.assertTrue(snap["identity_holds"])

    def test_refresh_kept_pending_while_stale_reads_continue(self):
        cache, origin, clock = make_cache()
        f0 = cache.get("k")
        origin.resolve("v1")
        f0.result(timeout=0)
        clock.advance(TTL + 0.5)

        first = cache.get("k")
        marker_of(first)  # first stale read arms the refresh
        clock.advance(1.0)
        second = cache.get("k")  # still stale, refresh in flight
        marker = marker_of(second)
        self.assertEqual(marker.stale_for, 1.5)
        self.assertEqual(marker.failures, 0)
        self.assertIsNone(marker.next_retry_at)
        self.assertEqual(origin.call_count, 2)  # no duplicate refresh

        origin.resolve("v2")
        clock.advance(0)
        self.assertEqual(value_of(cache.get("k")), "v2")


class FailureAndBackoffTests(unittest.TestCase):
    def test_failed_refresh_keeps_stale_value_and_retries_with_capped_backoff(self):
        cache, origin, clock = make_cache()
        f0 = cache.get("k")
        origin.resolve("v1")
        f0.result(timeout=0)

        self.assertEqual(cache.backoff_series(6), EXPECTED_SERIES)

        clock.advance(TTL + 0.1)
        marker_of(cache.get("k"))  # arm refresh #2
        origin.fail()
        clock.advance(0)

        # Stale value preserved with failure bookkeeping.
        m = marker_of(cache.get("k"))
        self.assertEqual(m.value, "v1")
        self.assertEqual(m.version, 1)
        self.assertEqual(m.failures, 1)
        self.assertAlmostEqual(m.next_retry_at, clock.now() + 1.0)
        self.assertEqual(cache.stats_snapshot()["refresh_failures"], 1)

        # Retry 2 after 1s, fails -> next interval 2s
        clock.advance(1.0)
        self.assertEqual(origin.call_count, 3)
        origin.fail()
        clock.advance(0)
        # Retry 3 after 2s, fails -> next interval 4s
        clock.advance(2.0)
        self.assertEqual(origin.call_count, 4)
        origin.fail()
        clock.advance(0)
        m = marker_of(cache.get("k"))
        self.assertEqual(m.failures, 3)
        self.assertAlmostEqual(m.next_retry_at, clock.now() + 4.0)
        # Retry 4 after 4s, fails -> next interval capped at 8s
        clock.advance(4.0)
        self.assertEqual(origin.call_count, 5)
        origin.fail()
        clock.advance(0)
        m = marker_of(cache.get("k"))
        self.assertEqual(m.failures, 4)
        self.assertAlmostEqual(m.next_retry_at, clock.now() + 8.0)
        # Cap stays 8s on the next failure.
        clock.advance(8.0)
        self.assertEqual(origin.call_count, 6)
        origin.fail()
        clock.advance(0)
        m = marker_of(cache.get("k"))
        self.assertEqual(m.failures, 5)
        self.assertAlmostEqual(m.next_retry_at, clock.now() + 8.0)

        # Eventually a retry succeeds: readers see the new value at once.
        clock.advance(8.0)
        self.assertEqual(origin.call_count, 7)
        origin.resolve("v2")
        clock.advance(0)
        self.assertEqual(value_of(cache.get("k")), "v2")
        self.assertEqual(cache.stats_snapshot()["refresh_failures"], 5)

    def test_origin_timeout_counts_as_failure_and_retry_succeeds(self):
        cache, origin, clock = make_cache()
        # First miss never resolves -> fetch times out; readers stay pending
        # (no value exists, so nothing stale can be served).
        first = cache.get("k")
        second = cache.get("k")  # coalesced
        self.assertEqual(origin.call_count, 1)

        clock.advance(TIMEOUT)
        self.assertIsInstance(first.exception(timeout=0), OriginTimeout)
        self.assertIsInstance(second.exception(timeout=0), OriginTimeout)
        snap = cache.stats_snapshot()
        self.assertEqual(snap["refresh_failures"], 1)
        self.assertTrue(snap["identity_holds"])

        # Backoff retry scheduled; coalesce onto it.
        third = cache.get("k")
        clock.advance(1.0)
        self.assertEqual(origin.call_count, 2)
        origin.resolve("v1")
        clock.advance(0)
        self.assertEqual(value_of(third), "v1")

    def test_timeout_does_not_race_with_late_origin_result(self):
        cache, origin, clock = make_cache()
        f0 = cache.get("k")
        origin.resolve("v1")
        f0.result(timeout=0)
        clock.advance(TTL + 0.1)
        marker_of(cache.get("k"))  # arm refresh

        clock.advance(TIMEOUT)  # refresh attempt times out
        # Origin then completes late: result must be ignored.
        origin.resolve("late")
        clock.advance(0)
        marker = marker_of(cache.get("k"))
        self.assertEqual(marker.value, "v1")
        self.assertEqual(marker.failures, 1)

        clock.advance(1.0)  # retry fires
        origin.resolve("v2")
        clock.advance(0)
        self.assertEqual(value_of(cache.get("k")), "v2")


class GraceWindowTests(unittest.TestCase):
    def test_readers_block_after_grace_window_and_coalesce(self):
        cache, origin, clock = make_cache()
        first = cache.get("k")
        origin.resolve("v1")
        first.result(timeout=0)

        clock.advance(TTL + 0.1)
        marker_of(cache.get("k"))  # arm refresh
        origin.fail()
        clock.advance(0)

        # Still inside the 30s grace window for a while. Retry timers fire at
        # 1, 2, 4, 8, 16 seconds; let each retry fail immediately.
        target = clock.now() + (GRACE - 1)
        while clock.now() < target:
            before = origin.call_count
            clock.advance(min(0.1, target - clock.now()))
            if origin.call_count > before:
                origin.fail()
                clock.advance(0)
        m = marker_of(cache.get("k"))
        self.assertEqual(m.value, "v1")

        # Grace ends without success: reads now block (no stale data served).
        clock.advance(1.0)
        n_before = origin.call_count
        blocked = [cache.get("k") for _ in range(3)]
        for fut in blocked:
            self.assertFalse(fut.done())
        # Hard reader cancels the backoff timer and fetches immediately;
        # the other two merge into that one call.
        self.assertEqual(origin.call_count, n_before + 1)
        snap = cache.stats_snapshot()
        self.assertEqual(snap["blocking_loads"], 2)  # first miss + this one
        self.assertEqual(snap["coalesced"], 2)
        self.assertTrue(snap["identity_holds"])

        origin.resolve("v2")
        clock.advance(0)
        for fut in blocked:
            self.assertEqual(value_of(fut), "v2")
        self.assertEqual(value_of(cache.get("k")), "v2")
        snap2 = cache.stats_snapshot()
        self.assertGreaterEqual(snap2["refresh_failures"], 1)
        self.assertTrue(snap2["identity_holds"])

    def test_first_miss_blocking_after_failure_then_success(self):
        cache, origin, clock = make_cache()
        fut = cache.get("k")
        origin.fail()
        clock.advance(0)  # failure recorded
        self.assertIsInstance(fut.exception(timeout=0), OriginError)

        # A new read with no value available is a hard reader: it cancels the
        # backoff wait and triggers an immediate retry (merging with it).
        waiting = cache.get("k")
        self.assertFalse(waiting.done())
        self.assertEqual(origin.call_count, 2)
        origin.resolve("v1")
        clock.advance(0)
        self.assertEqual(value_of(waiting), "v1")


class ImmediacyTests(unittest.TestCase):
    def test_new_value_is_visible_immediately_with_incremented_version(self):
        cache, origin, clock = make_cache()
        f0 = cache.get("k")
        origin.resolve("v1")
        self.assertEqual(value_of(f0), "v1")

        clock.advance(TTL + 1)
        marker = marker_of(cache.get("k"))
        self.assertEqual(marker.version, 1)
        origin.resolve("v2")
        clock.advance(0)

        fresh = cache.get("k").result(timeout=0)
        self.assertEqual(fresh, "v2")
        clock.advance(TTL - 1)
        self.assertEqual(cache.get("k").result(timeout=0), "v2")  # still fresh


class ThreadedSmokeTests(unittest.TestCase):
    def test_real_threads_coalesce_concurrent_misses(self):
        release = threading.Event()

        def loader(key, version):
            release.wait(5)
            return "v1"

        origin = ThreadedOrigin(loader)
        cache = SWRCache(
            origin=origin,
            ttl=10.0,
            grace=30.0,
            fetch_timeout=5.0,
            backoff=BackoffPolicy(0.01, 2.0, 0.1),
            scheduler=ThreadScheduler(),
        )
        results: list = []
        errors: list = []

        def worker():
            try:
                results.append(cache.get("k").result(timeout=5))
            except BaseException as exc:  # pragma: no cover - failure signal
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        # Give workers time to converge on the cache before releasing origin.
        release.set()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(results, ["v1"] * 5)
        snap = cache.stats_snapshot()
        self.assertEqual(snap["origin_calls"], 1)
        self.assertEqual(snap["blocking_loads"], 1)
        self.assertEqual(snap["coalesced"], 4)
        self.assertTrue(snap["identity_holds"])
        origin.close()


class StatsIdentityTests(unittest.TestCase):
    def test_stats_are_self_consistent_across_mixed_traffic(self):
        cache, origin, clock = make_cache()
        f_a = cache.get("a")
        origin.resolve("a1")
        f_a.result(timeout=0)
        clock.advance(1)
        cache.get("a").result(timeout=0)  # hit

        b_futs = [cache.get("b") for _ in range(3)]  # 1 load + 2 coalesced
        origin.resolve("b1")
        clock.advance(0)
        for f in b_futs:
            f.result(timeout=0)

        clock.advance(TTL + 1)
        [cache.get("a") for _ in range(2)]  # stale + stale
        origin.resolve("a2")
        clock.advance(0)

        snap = cache.stats_snapshot()
        self.assertEqual(snap["requests"], 1 + 1 + 3 + 2)
        self.assertEqual(snap["hits"], 1)
        self.assertEqual(snap["stale_hits"], 2)
        self.assertEqual(snap["blocking_loads"], 2)
        self.assertEqual(snap["coalesced"], 2)
        self.assertEqual(
            snap["requests"],
            snap["hits"] + snap["stale_hits"] + snap["coalesced"] + snap["blocking_loads"],
        )
        self.assertTrue(snap["identity_holds"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
