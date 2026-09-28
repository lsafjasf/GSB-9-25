"""Request/response correlation layer over a SimChannel (stdlib only).

Guarantees:
- unique id per request; responses matched strictly by id
- unknown / duplicate / late(expired) responses are dropped and counted
- timeout isolation: a timed-out request is tombstoned; a late response
  can never rewrite state
- bounded concurrency (fail-fast or block) and explicit cancel with
  immediate table/buffer reclamation
- deterministic outcome on disconnect (fail or resend-on-reconnect);
  every wait is bounded by a mandatory timeout, so nothing waits forever
"""

import itertools
import threading
import time
from dataclasses import dataclass

from channel import ChannelDownError


class CorrelatorError(Exception):
    """Base class for correlator errors."""


class RequestTimeout(CorrelatorError):
    """The request did not complete within its timeout."""


class RequestCancelled(CorrelatorError):
    """The request was explicitly cancelled."""


class OverloadError(CorrelatorError):
    """In-flight limit reached and policy is fail-fast."""


@dataclass
class Stats:
    sent: int = 0          # requests registered
    succeeded: int = 0
    timed_out: int = 0
    failed: int = 0        # channel down / resend exhausted
    cancelled: int = 0
    rejected: int = 0      # fast-fail on in-flight limit
    unknown_id: int = 0    # response id never seen (or tombstone expired)
    duplicate: int = 0     # response for an already-completed request
    late: int = 0          # response after timeout/cancel/failure
    resent: int = 0        # retransmissions after reconnect

    @property
    def completed(self):
        return self.succeeded + self.timed_out + self.failed + self.cancelled


class _Entry:
    __slots__ = ("request_id", "payload", "event", "result", "error",
                 "resends", "slot_released")

    def __init__(self, request_id, payload):
        self.request_id = request_id
        self.payload = payload
        self.event = threading.Event()
        self.result = None
        self.error = None
        self.resends = 0
        self.slot_released = False


class RequestHandle:
    def __init__(self, correlator, entry):
        self._corr = correlator
        self._entry = entry
        self.request_id = entry.request_id

    @property
    def done(self):
        return self._entry.event.is_set()

    def result(self, timeout):
        """Block up to `timeout` seconds; always terminates."""
        entry = self._entry
        try:
            if not entry.event.wait(timeout):
                self._corr._timeout(entry)
            if entry.event.is_set():
                if entry.error is not None:
                    raise entry.error
                return entry.result
            raise RequestTimeout(
                f"request {self.request_id} timed out after {timeout}s")
        finally:
            self._corr._release_slot(entry)

    def cancel(self):
        return self._corr.cancel(self.request_id)


class Correlator:
    def __init__(self, channel, max_inflight=256, overload="fail",
                 disconnect_policy="fail", max_resends=3, tombstone_ttl=30.0):
        if overload not in ("fail", "block"):
            raise ValueError("overload must be 'fail' or 'block'")
        if disconnect_policy not in ("fail", "resend"):
            raise ValueError("disconnect_policy must be 'fail' or 'resend'")
        self._channel = channel
        self._overload = overload
        self._disconnect_policy = disconnect_policy
        self._max_resends = max_resends
        self._tombstone_ttl = tombstone_ttl
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._pending = {}       # id -> _Entry
        self._tombstones = {}    # id -> (kind, expiry) kind in done/timeout/cancelled/failed
        self._last_purge = time.monotonic()
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.stats = Stats()
        channel.set_receiver(self.handle_incoming)
        channel.set_disconnect_hook(self._on_disconnect)
        channel.set_reconnect_hook(self._on_reconnect)

    # -- introspection ----------------------------------------------------
    @property
    def inflight(self):
        with self._lock:
            return len(self._pending)

    @property
    def tombstone_count(self):
        with self._lock:
            return len(self._tombstones)

    # -- issuing requests ---------------------------------------------------
    def submit(self, payload):
        """Register a request and put it on the wire; returns a handle."""
        if self._overload == "fail":
            if not self._slots.acquire(blocking=False):
                with self._lock:
                    self.stats.rejected += 1
                raise OverloadError("in-flight limit reached")
        else:
            self._slots.acquire()
        request_id = next(self._ids)
        entry = _Entry(request_id, payload)
        with self._lock:
            self._pending[request_id] = entry
            self.stats.sent += 1
        try:
            self._channel.send({"id": request_id, "payload": payload})
        except ChannelDownError as exc:
            self._fail_entry(entry, exc)
        return RequestHandle(self, entry)

    def request(self, payload, timeout):
        """Synchronous convenience wrapper: submit + wait."""
        return self.submit(payload).result(timeout)

    def cancel(self, request_id):
        """Cancel a request; the correlation entry is reclaimed immediately."""
        with self._lock:
            entry = self._pending.pop(request_id, None)
            if entry is None:
                return False
            self._tombstone_locked(request_id, "cancelled")
            self.stats.cancelled += 1
            entry.error = RequestCancelled(f"request {request_id} cancelled")
            entry.event.set()
        self._release_slot(entry)
        return True

    # -- incoming responses -------------------------------------------------
    def handle_incoming(self, msg):
        request_id = msg.get("id")
        with self._lock:
            self._purge_tombstones_locked()
            entry = self._pending.pop(request_id, None)
            if entry is not None:
                self._tombstone_locked(request_id, "done")
                self.stats.succeeded += 1
                entry.result = msg.get("payload")
                entry.event.set()
            else:
                tomb = self._tombstones.get(request_id)
                if tomb is None:
                    self.stats.unknown_id += 1
                elif tomb[0] == "done":
                    self.stats.duplicate += 1
                else:
                    self.stats.late += 1
                return
        self._release_slot(entry)

    # -- disconnect / reconnect ---------------------------------------------
    def _on_disconnect(self):
        if self._disconnect_policy != "fail":
            return  # resend policy: entries stay pending until reconnect/timeout
        with self._lock:
            entries = list(self._pending.values())
        for entry in entries:
            self._fail_entry(entry, ChannelDownError("channel disconnected"))

    def _on_reconnect(self):
        if self._disconnect_policy != "resend":
            return
        with self._lock:
            entries = list(self._pending.values())
        for entry in entries:
            entry.resends += 1
            if entry.resends > self._max_resends:
                self._fail_entry(
                    entry, ChannelDownError("resend limit exceeded"))
                continue
            try:
                self._channel.send({"id": entry.request_id,
                                    "payload": entry.payload})
                with self._lock:
                    self.stats.resent += 1
            except ChannelDownError:
                pass  # still down; retried on next reconnect or timed out

    # -- terminal transitions -------------------------------------------------
    def _timeout(self, entry):
        with self._lock:
            if self._pending.pop(entry.request_id, None) is None:
                return  # completed concurrently; the response wins
            self._tombstone_locked(entry.request_id, "timeout")
            self.stats.timed_out += 1
        self._release_slot(entry)

    def _fail_entry(self, entry, exc):
        with self._lock:
            if self._pending.pop(entry.request_id, None) is None:
                return
            self._tombstone_locked(entry.request_id, "failed")
            self.stats.failed += 1
            entry.error = exc
            entry.event.set()
        self._release_slot(entry)

    def _release_slot(self, entry):
        with self._lock:
            if entry.slot_released:
                return
            entry.slot_released = True
        self._slots.release()

    # -- tombstones (bounded memory for late/duplicate detection) ------------
    def _tombstone_locked(self, request_id, kind):
        self._tombstones[request_id] = (
            kind, time.monotonic() + self._tombstone_ttl)
        self._purge_tombstones_locked()

    def _purge_tombstones_locked(self):
        now = time.monotonic()
        if now - self._last_purge < 0.1:
            return
        self._last_purge = now
        expired = [k for k, (_, exp) in self._tombstones.items() if exp <= now]
        for k in expired:
            del self._tombstones[k]

    def purge_expired(self):
        with self._lock:
            self._last_purge = 0.0
            self._purge_tombstones_locked()
