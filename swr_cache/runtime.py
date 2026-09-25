"""Clock/scheduler implementations and an origin adapter for real threads."""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from .cache import Future, OriginError


class ThreadScheduler:
    """Wall-clock scheduler backed by threading.Timer."""

    def now(self) -> float:
        return time.monotonic()

    def schedule(self, when: float, callback: Callable[[], None]) -> threading.Timer:
        timer = threading.Timer(max(0.0, when - self.now()), callback)
        timer.daemon = True
        timer.start()
        return timer

    def cancel(self, token: Any) -> None:
        token.cancel()


class ThreadedOrigin:
    """Adapt a plain ``loader(key, version) -> value`` to the origin protocol."""

    def __init__(
        self,
        loader: Callable[[str, int], Any],
        max_workers: int = 8,
    ) -> None:
        self._loader = loader
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def fetch(self, key: str, current_version: int) -> Future:
        fut: Future = Future()

        def work() -> None:
            try:
                fut.set_result(self._loader(key, current_version))
            except BaseException as exc:  # noqa: BLE001 - propagate any loader failure
                if not isinstance(exc, Exception):
                    raise
                fut.set_exception(OriginError(str(exc)) if not isinstance(exc, OriginError) else exc)

        self._pool.submit(work)
        return fut

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class _VTToken:
    __slots__ = ("when", "seq", "callback", "cancelled")

    def __init__(self, when: float, seq: int, callback: Callable[[], None]) -> None:
        self.when = when
        self.seq = seq
        self.callback = callback
        self.cancelled = False


class VirtualRuntime:
    """Deterministic virtual clock and timer queue for tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._time = start
        self._heap: list[_VTToken] = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self._time

    def schedule(self, when: float, callback: Callable[[], None]) -> _VTToken:
        token = _VTToken(when, next(self._seq), callback)
        heapq.heappush(self._heap, (when, token.seq, token))
        return token

    def cancel(self, token: Any) -> None:
        token.cancelled = True

    def pending(self) -> int:
        return sum(1 for t in self._heap if not t[2].cancelled)

    def advance(self, seconds: float) -> None:
        """Move the clock forward, firing every timer up to ``time + seconds``.

        Timers scheduled (or rescheduled) by fired callbacks are also processed,
        including a retry that schedules the next attempt + its timeout.
        """
        target = self._time + seconds
        while self._heap:
            when, _seq, token = heapq.heappop(self._heap)
            if token.cancelled:
                continue
            if when > target:
                heapq.heappush(self._heap, (when, _seq, token))
                break
            self._time = when
            token.callback()
        self._time = target
