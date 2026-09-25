"""Self-tests for the request/response correlation layer.

Run:  python3 -m unittest test_correlator -v
"""

import gc
import resource
import threading
import time
import tracemalloc
import unittest
from concurrent.futures import ThreadPoolExecutor

from channel import SimChannel
from correlator import (Correlator, CorrelatorError, OverloadError,
                        RequestCancelled, RequestTimeout)
from channel import ChannelDownError


def echo_peer(msg):
    return {"id": msg["id"], "payload": msg["payload"]}


def make(latency=0.001, jitter=0.0, loss_rate=0.0, replay_rate=0.0,
         seed=1, **kw):
    ch = SimChannel(latency=latency, jitter=jitter, loss_rate=loss_rate,
                    replay_rate=replay_rate, seed=seed)
    ch.set_peer(echo_peer)
    return ch, Correlator(ch, **kw)


class BasicTest(unittest.TestCase):
    def test_basic_request_response(self):
        ch, c = make()
        self.assertEqual(c.request("ping", timeout=1.0), "ping")
        self.assertEqual(c.stats.succeeded, 1)
        self.assertEqual(c.inflight, 0)
        ch.close()

    def test_unique_ids(self):
        ch, c = make()
        handles = [c.submit(i) for i in range(10)]
        ids = {h.request_id for h in handles}
        self.assertEqual(len(ids), 10)
        for h in handles:
            h.result(1.0)
        ch.close()


class ReorderTest(unittest.TestCase):
    def test_out_of_order_responses_matched_correctly(self):
        # heavy jitter shuffles response order on the wire
        ch, c = make(latency=0.001, jitter=0.05, seed=42)
        n = 300
        with ThreadPoolExecutor(max_workers=32) as ex:
            results = list(ex.map(lambda i: c.request(i, timeout=2.0),
                                  range(n)))
        self.assertEqual(results, list(range(n)))  # every response matched
        self.assertEqual(c.stats.succeeded, n)
        self.assertEqual(c.inflight, 0)
        self.assertEqual(ch.delivered, n)
        ch.close()


class TimeoutIsolationTest(unittest.TestCase):
    def test_timeout_is_immediate_and_late_response_is_dropped(self):
        ch, c = make(latency=0.25)  # response arrives long after timeout
        t0 = time.monotonic()
        with self.assertRaises(RequestTimeout):
            c.request("x", timeout=0.05)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.20, "caller must be released immediately")
        self.assertEqual(c.stats.timed_out, 1)
        self.assertEqual(c.inflight, 0)

        time.sleep(0.6)  # let the late response arrive (RTT = 2x latency)
        self.assertEqual(c.stats.late, 1, "late response must be counted")
        # ... and must not rewrite any state:
        self.assertEqual(c.stats.succeeded, 0)
        self.assertEqual(c.stats.timed_out, 1)
        self.assertEqual(c.inflight, 0)

        # a subsequent request is unaffected
        ch.latency = 0.001
        self.assertEqual(c.request("y", timeout=1.0), "y")
        self.assertEqual(c.stats.succeeded, 1)
        self.assertEqual(c.stats.timed_out, 1)
        ch.close()


class StrayResponseTest(unittest.TestCase):
    def test_unknown_id_counted(self):
        ch, c = make()
        c.handle_incoming({"id": 999999, "payload": None})
        self.assertEqual(c.stats.unknown_id, 1)
        ch.close()

    def test_duplicate_response_counted(self):
        ch, c = make()
        h = c.submit("x")
        self.assertEqual(h.result(1.0), "x")
        # replayed copy of the same response arrives again
        c.handle_incoming({"id": h.request_id, "payload": "x"})
        self.assertEqual(c.stats.duplicate, 1)
        self.assertEqual(c.stats.succeeded, 1)
        ch.close()

    def test_replay_on_wire_counted(self):
        ch, c = make(replay_rate=1.0)  # every response duplicated by channel
        self.assertEqual(c.request("x", timeout=1.0), "x")
        time.sleep(0.1)
        self.assertGreaterEqual(c.stats.duplicate, 1)
        ch.close()


class ConcurrencyLimitTest(unittest.TestCase):
    def test_fail_fast_when_limit_reached(self):
        ch, c = make(latency=0.2, max_inflight=2, overload="fail")
        h1, h2 = c.submit(1), c.submit(2)
        with self.assertRaises(OverloadError):
            c.submit(3)
        self.assertEqual(c.stats.rejected, 1)
        self.assertEqual(h1.result(1.0), 1)
        self.assertEqual(h2.result(1.0), 2)
        ch.close()

    def test_block_mode_waits_for_slot(self):
        ch, c = make(latency=0.15, max_inflight=1, overload="block")
        h1 = c.submit("a")
        holder = {}

        def second():
            t0 = time.monotonic()
            h2 = c.submit("b")  # blocks until h1 releases its slot
            holder["waited"] = time.monotonic() - t0
            holder["result"] = h2.result(1.0)

        t = threading.Thread(target=second)
        t.start()
        self.assertEqual(h1.result(1.0), "a")
        t.join(2.0)
        self.assertEqual(holder["result"], "b")
        self.assertGreater(holder["waited"], 0.05)
        ch.close()


class CancelTest(unittest.TestCase):
    def test_cancel_reclaims_immediately(self):
        ch, c = make(latency=0.3)
        h = c.submit("x")
        time.sleep(0.02)
        self.assertTrue(h.cancel())
        self.assertEqual(c.inflight, 0, "table entry must be gone at once")
        with self.assertRaises(RequestCancelled):
            h.result(1.0)
        self.assertFalse(h.cancel())  # already gone
        # slot was released: a new request under max_inflight=1 would fit
        self.assertEqual(c.stats.cancelled, 1)
        time.sleep(0.7)  # late response for the cancelled id (RTT = 2x latency)
        self.assertEqual(c.stats.late, 1)
        self.assertEqual(c.stats.succeeded, 0)
        ch.close()


class DisconnectTest(unittest.TestCase):
    def test_disconnect_fails_inflight_immediately(self):
        ch, c = make(latency=0.3, disconnect_policy="fail")
        h = c.submit("x")
        time.sleep(0.05)
        ch.disconnect()
        t0 = time.monotonic()
        with self.assertRaises(ChannelDownError):
            h.result(5.0)  # must not wait for the timeout
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(c.stats.failed, 1)
        self.assertEqual(c.inflight, 0)
        ch.reconnect()
        ch.latency = 0.001
        self.assertEqual(c.request("y", timeout=1.0), "y")
        ch.close()

    def test_send_while_disconnected_fails_fast(self):
        ch, c = make(disconnect_policy="fail")
        ch.disconnect()
        with self.assertRaises(ChannelDownError):
            c.request("x", timeout=1.0)
        self.assertEqual(c.stats.failed, 1)
        self.assertEqual(c.inflight, 0)
        ch.close()

    def test_reconnect_resends_inflight(self):
        ch, c = make(latency=0.2, disconnect_policy="resend")
        h = c.submit("x")
        time.sleep(0.05)
        ch.disconnect()      # in-flight wire messages are dropped
        time.sleep(0.05)
        ch.reconnect()       # correlator retransmits
        self.assertEqual(h.result(2.0), "x")
        self.assertGreaterEqual(c.stats.resent, 1)
        self.assertEqual(c.stats.succeeded, 1)
        ch.close()


class StressTest(unittest.TestCase):
    def test_stress_100k_with_loss_and_reorder(self):
        N = 100_000
        ch, c = make(latency=0.0005, jitter=0.003, loss_rate=0.01,
                     replay_rate=0.01, seed=7, max_inflight=4096,
                     overload="fail", tombstone_ttl=0.5)
        # warmup, then take the memory baseline
        for i in range(500):
            try:
                c.request(i, timeout=1.0)
            except CorrelatorError:
                pass
        gc.collect()
        tracemalloc.start()
        base, _ = tracemalloc.get_traced_memory()
        s0 = c.stats
        sent0, ok0, to0, fail0 = s0.sent, s0.succeeded, s0.timed_out, s0.failed

        outcomes = {"ok": 0, "timeout": 0, "failed": 0}
        lock = threading.Lock()

        def work(i):
            try:
                r = c.request(i, timeout=0.3)
                assert r == i, f"mismatched response: {r} != {i}"
                key = "ok"
            except RequestTimeout:
                key = "timeout"
            except CorrelatorError:
                key = "failed"
            with lock:
                outcomes[key] += 1

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=64) as ex:
            list(ex.map(work, range(N)))
        duration = time.monotonic() - t0

        gc.collect()
        time.sleep(0.7)          # let tombstones expire
        c.purge_expired()
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        s = c.stats

        print(f"\n--- stress report ---")
        print(f"requests={N} duration={duration:.2f}s "
              f"({N / duration:.0f} req/s)")
        print(f"outcomes: {outcomes}")
        print(f"stats: sent={s.sent} ok={s.succeeded} timeout={s.timed_out} "
              f"failed={s.failed} rejected={s.rejected}")
        print(f"wire: unknown={s.unknown_id} dup={s.duplicate} "
              f"late={s.late} | channel: sent={ch.sent} dropped={ch.dropped} "
              f"delivered={ch.delivered} replayed={ch.replayed}")
        print(f"memory: baseline={base / 1e6:.2f}MB peak={peak / 1e6:.2f}MB "
              f"after={current / 1e6:.2f}MB max_rss={rss_kb / 1024:.1f}MB")
        print(f"residual: inflight={c.inflight} "
              f"tombstones={c.tombstone_count}")

        # statistics must be self-consistent (deltas exclude warmup)
        self.assertEqual(outcomes["ok"], s.succeeded - ok0)
        self.assertEqual(outcomes["timeout"], s.timed_out - to0)
        self.assertEqual(outcomes["failed"], s.failed - fail0)
        self.assertEqual((s.succeeded - ok0) + (s.timed_out - to0)
                         + (s.failed - fail0), N)
        self.assertEqual(s.sent - sent0, N)
        self.assertEqual(s.completed, s.sent)
        self.assertEqual(s.rejected, 0)
        self.assertGreater(s.timed_out, 0, "loss must produce timeouts")
        self.assertGreater(s.duplicate, 0, "replay must produce duplicates")
        # no dangling state, memory falls back
        self.assertEqual(c.inflight, 0)
        self.assertEqual(c.tombstone_count, 0)
        self.assertLess(current - base, 4 * 1024 * 1024,
                        "memory must fall back to baseline")
        ch.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
