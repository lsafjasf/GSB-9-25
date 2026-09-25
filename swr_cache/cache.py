"""Stale-while-revalidate cache.

Features
--------
* Fresh entries are served directly.
* Expired entries stay readable inside a configurable grace window; the first
  stale read triggers exactly one background refresh.
* Concurrent misses for one key are coalesced into a single origin call.
* After the grace window, readers block on the in-flight (or scheduled retry)
  fetch instead of stampeding the origin.
* Failed refreshes keep the stale value and retry with exponential backoff,
  capped at a maximum interval.
* Origin calls can time out; timeouts are treated as refresh failures.
* Clock and scheduler are injectable, so tests run on virtual time.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Protocol



class OriginError(Exception):
    """Origin reported a failure."""


class OriginTimeout(Exception):
    """Origin call did not complete before the configured timeout."""


class Future:
    """Minimal thread-safe future (subset of concurrent.futures.Future)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._result: Any = None
        self._exc: Optional[BaseException] = None
        self._callbacks: List[Callable[["Future"], None]] = []

    def done(self) -> bool:
        return self._event.is_set()

    def set_result(self, result: Any) -> None:
        self._settle(result=result)

    def set_exception(self, exc: BaseException) -> None:
        self._settle(exc=exc)

    def _settle(self, result: Any = None, exc: Optional[BaseException] = None) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._result = result
            self._exc = exc
            self._event.set()
            callbacks = self._callbacks
            self._callbacks = []
        for cb in callbacks:
            cb(self)

    def result(self, timeout: Optional[float] = None) -> Any:
        if not self._event.wait(timeout):
            raise TimeoutError("future did not complete in time")
        if self._exc is not None:
            raise self._exc
        return self._result

    def exception(self, timeout: Optional[float] = None) -> Optional[BaseException]:
        if not self._event.wait(timeout):
            raise TimeoutError("future did not complete in time")
        return self._exc

    def add_done_callback(self, cb: Callable[["Future"], None]) -> None:
        run_now = False
        with self._lock:
            if self._event.is_set():
                run_now = True
            else:
                self._callbacks.append(cb)
        if run_now:
            cb(self)


@dataclass(frozen=True)
class StaleMarker:
    """Wrapper returned for reads served from an expired entry."""

    value: Any
    version: int
    expired_at: float
    stale_for: float
    failures: int
    next_retry_at: Optional[float]

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "version": self.version,
            "expired_at": self.expired_at,
            "stale_for": self.stale_for,
            "failures": self.failures,
            "next_retry_at": self.next_retry_at,
        }


@dataclass(frozen=True)
class Entry:
    value: Any
    version: int
    created_at: float
    ttl: float

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl


@dataclass
class Stats:
    requests: int = 0
    hits: int = 0
    stale_hits: int = 0
    coalesced: int = 0
    blocking_loads: int = 0
    origin_calls: int = 0
    refresh_failures: int = 0

    def as_dict(self) -> dict:
        return dict(
            requests=self.requests,
            hits=self.hits,
            stale_hits=self.stale_hits,
            origin_calls=self.origin_calls,
            coalesced=self.coalesced,
            refresh_failures=self.refresh_failures,
            blocking_loads=self.blocking_loads,
        )

    def identity_holds(self) -> bool:
        """Every request is fresh, stale, coalesced, or starts a blocking load."""
        return (
            self.requests
            == self.hits + self.stale_hits + self.coalesced + self.blocking_loads
        )


class BackoffPolicy:
    """delay(k) = min(base_delay * factor ** (k - 1), max_delay), k >= 1."""

    def __init__(
        self,
        base_delay: float = 0.1,
        factor: float = 2.0,
        max_delay: float = 5.0,
    ) -> None:
        if base_delay < 0 or factor < 1 or max_delay < base_delay:
            raise ValueError("invalid backoff parameters")
        self.base_delay = base_delay
        self.factor = factor
        self.max_delay = max_delay

    def delay(self, attempt: int) -> float:
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        return min(self.base_delay * self.factor ** (attempt - 1), self.max_delay)

    def series(self, attempts: int) -> List[float]:
        """Intervals before attempts 1..`attempts` (first one is the immediate retry)."""
        return [self.delay(k) for k in range(1, attempts + 1)]


class Origin(Protocol):
    def fetch(self, key: str, current_version: int) -> Future: ...


class Scheduler(Protocol):
    def now(self) -> float: ...

    def schedule(self, when: float, callback: Callable[[], None]) -> Any: ...

    def cancel(self, token: Any) -> None: ...


class _Slot:
    __slots__ = (
        "entry",
        "expires_at",
        "version",
        "inflight_kind",
        "attempt_id",
        "attempt_settled",
        "attempt_timeout_token",
        "attempt_handle",
        "failures",
        "retry_token",
        "retry_at",
        "waiters",
    )

    def __init__(self) -> None:
        self.entry: Optional[Entry] = None
        self.expires_at: float = 0.0
        self.version: int = 0
        self.inflight_kind: Optional[str] = None
        self.attempt_id: int = 0
        self.attempt_settled: bool = True
        self.attempt_timeout_token: Any = None
        self.attempt_handle: Optional[Future] = None
        self.failures: int = 0
        self.retry_token: Any = None
        self.retry_at: Optional[float] = None
        self.waiters: List[Future] = []

    def load_pending(self) -> bool:
        return self.inflight_kind is not None or self.retry_token is not None


class SWRCache:
    """Stale-while-revalidate cache.

    Parameters
    ----------
    origin:
        Object exposing ``fetch(key, current_version) -> Future``.
    ttl:
        Fresh lifetime of a value.
    grace:
        How long after expiry a stale value is still served while a background
        refresh runs. Reads after this block until a value is available.
    fetch_timeout:
        Per origin-call timeout; a timeout counts as a refresh failure.
    backoff:
        Retry policy used after refresh failures.
    scheduler:
        Injectable clock/timer. Defaults to a real threaded scheduler.
    """

    def __init__(
        self,
        origin: Origin,
        ttl: float,
        grace: float,
        fetch_timeout: float = 1.0,
        backoff: Optional[BackoffPolicy] = None,
        scheduler: Optional[Scheduler] = None,
    ) -> None:
        if ttl <= 0 or grace < 0 or fetch_timeout <= 0:
            raise ValueError("ttl and fetch_timeout must be > 0, grace must be >= 0")
        self._origin = origin
        self._ttl = ttl
        self._grace = grace
        self._fetch_timeout = fetch_timeout
        self._backoff = backoff or BackoffPolicy()
        self._scheduler = scheduler or ThreadScheduler()
        self._lock = threading.RLock()
        self._slots: dict[str, _Slot] = {}
        self.stats = Stats()

    # ------------------------------------------------------------------ API

    def get(self, key: str) -> Future:
        """Return a Future resolving to a value, or a StaleMarker wrapper.

        Fresh reads resolve immediately with the raw value; stale reads (inside
        the grace window) resolve immediately with a ``StaleMarker``; reads
        without any usable value resolve once the origin returns, or fail with
        the last origin exception if the cache is closed mid-load.
        """
        now = self._scheduler.now()
        with self._lock:
            self.stats.requests += 1
            slot = self._slots.get(key)
            if slot is None:
                slot = _Slot()
                self._slots[key] = slot

            if slot.entry is not None and now < slot.expires_at:
                self.stats.hits += 1
                fut: Future = Future()
                fut.set_result(slot.entry.value)
                return fut

            if (
                slot.entry is not None
                and now <= slot.expires_at + self._grace
            ):
                self.stats.stale_hits += 1
                self._ensure_refresh(slot, key, now)
                result = Future()
                result.set_result(self._marker(slot, now))
                return result

            # No value available at all, or stale value past the grace window.
            if slot.inflight_kind is not None or slot.waiters:
                # A fetch is in flight or earlier hard readers are waiting;
                # merge into the same origin call.
                self.stats.coalesced += 1
            else:
                # Only a backoff timer may be pending: a hard reader is not
                # willing to wait for it, so retry immediately.
                self.stats.blocking_loads += 1
            waiter = Future()
            slot.waiters.append(waiter)
            if slot.inflight_kind is None:
                self._cancel_retry(slot)
                self._start_fetch(slot, key, "refresh" if slot.entry else "miss", now)
            return waiter

    def backoff_series(self, attempts: int) -> List[float]:
        return self._backoff.series(attempts)

    def stats_snapshot(self) -> dict:
        with self._lock:
            snap = self.stats.as_dict()
            snap["identity_holds"] = self.stats.identity_holds()
            return snap

    # -------------------------------------------------------------- internals

    def _marker(self, slot: _Slot, now: float) -> StaleMarker:
        return StaleMarker(
            value=slot.entry.value,
            version=slot.entry.version,
            expired_at=slot.expires_at,
            stale_for=now - slot.expires_at,
            failures=slot.failures,
            next_retry_at=slot.retry_at,
        )

    def _ensure_refresh(self, slot: _Slot, key: str, now: float) -> None:
        """Start a background refresh if none is running or scheduled."""
        if slot.load_pending():
            return
        self._start_fetch(slot, key, "refresh", now)

    def _cancel_retry(self, slot: _Slot) -> None:
        if slot.retry_token is not None:
            self._scheduler.cancel(slot.retry_token)
            slot.retry_token = None
            slot.retry_at = None

    def _start_fetch(self, slot: _Slot, key: str, kind: str, now: float) -> None:
        self._cancel_retry(slot)
        slot.inflight_kind = kind
        slot.attempt_id += 1
        attempt_id = slot.attempt_id
        slot.attempt_settled = False
        self.stats.origin_calls += 1
        slot.attempt_timeout_token = self._scheduler.schedule(
            now + self._fetch_timeout,
            lambda: self._on_timeout(key, attempt_id),
        )
        try:
            handle = self._origin.fetch(key, slot.version)
        except BaseException as exc:  # origin raising synchronously
            slot.attempt_handle = None
            self._settle_attempt(slot, attempt_id)
            self._fetch_failed(slot, key, attempt_id, exc, now)
            return
        slot.attempt_handle = handle
        handle.add_done_callback(
            lambda h: self._on_origin_done(key, attempt_id, h)
        )

    def _settle_attempt(self, slot: _Slot, attempt_id: int) -> bool:
        """Return True exactly once per attempt; disarm the timeout timer."""
        if slot.attempt_id != attempt_id or slot.attempt_settled:
            return False
        slot.attempt_settled = True
        if slot.attempt_timeout_token is not None:
            self._scheduler.cancel(slot.attempt_timeout_token)
            slot.attempt_timeout_token = None
        return True

    def _on_timeout(self, key: str, attempt_id: int) -> None:
        now = self._scheduler.now()
        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                return
            if not self._settle_attempt(slot, attempt_id):
                return
            slot.attempt_handle = None
            slot.inflight_kind = None
            self._fetch_failed(slot, key, attempt_id, OriginTimeout(), now)

    def _on_origin_done(self, key: str, attempt_id: int, handle: Future) -> None:
        now = self._scheduler.now()
        with self._lock:
            slot = self._slots.get(key)
            if slot is None or not self._settle_attempt(slot, attempt_id):
                return  # superseded (e.g. timeout already fired); ignore
            slot.attempt_handle = None
            exc = handle.exception(timeout=0)
            if exc is not None:
                self._fetch_failed(slot, key, attempt_id, exc, now)
            else:
                self._fetch_succeeded(slot, attempt_id, handle.result(timeout=0), now)

    def _fetch_succeeded(
        self, slot: _Slot, attempt_id: int, value: Any, now: float
    ) -> None:
        slot.inflight_kind = None
        slot.failures = 0
        slot.version += 1
        slot.entry = Entry(
            value=value, version=slot.version, created_at=now, ttl=self._ttl
        )
        slot.expires_at = slot.entry.expires_at
        waiters = slot.waiters
        slot.waiters = []
        for waiter in waiters:
            waiter.set_result(value)

    def _fetch_failed(
        self,
        slot: _Slot,
        key: str,
        attempt_id: int,
        exc: BaseException,
        now: float,
    ) -> None:
        slot.inflight_kind = None
        slot.failures += 1
        self.stats.refresh_failures += 1
        delay = self._backoff.delay(slot.failures)
        slot.retry_at = now + delay

        def retry() -> None:
            tick = self._scheduler.now()
            with self._lock:
                cur = self._slots.get(key)
                if cur is not slot or slot.attempt_id != attempt_id:
                    return
                if slot.retry_token is None or slot.inflight_kind is not None:
                    return
                slot.retry_token = None
                slot.retry_at = None
                kind = "refresh" if slot.entry else "miss"
                self._start_fetch(slot, key, kind, tick)

        token = self._scheduler.schedule(now + delay, retry)
        slot.retry_token = token
        # Hard readers (no value exists) cannot be served stale: surface the
        # error immediately. They are free to call get() again and merge onto
        # the scheduled retry.
        if slot.entry is None:
            self._fail_all_waiters(slot, exc)

    def _fail_all_waiters(self, slot: _Slot, exc: BaseException) -> None:
        waiters = slot.waiters
        slot.waiters = []
        for waiter in waiters:
            waiter.set_exception(exc)

    def close(self) -> None:
        """Cancel scheduled retries; fail readers still waiting for a value."""
        with self._lock:
            for slot in self._slots.values():
                self._cancel_retry(slot)
                if slot.entry is None:
                    self._fail_all_waiters(slot, OriginError("cache closed"))
