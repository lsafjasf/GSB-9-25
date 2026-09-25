"""Programmable origin and assertion helpers for virtual-time tests."""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from swr_cache.cache import Future, OriginError, StaleMarker


class Call:
    """One in-flight origin invocation, completed manually by the test."""

    def __init__(self, key: str, version: int) -> None:
        self.key = key
        self.version = version
        self.future = Future()


class FakeOrigin:
    """Origin whose calls the test resolves or fails on demand."""

    def __init__(
        self,
        script: Optional[List[Callable[[int], Any]]] = None,
    ) -> None:
        self.calls: List[Call] = []
        self._script = list(script or [])

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last(self) -> Call:
        return self.calls[-1]

    def fetch(self, key: str, current_version: int) -> Future:
        call = Call(key, current_version)
        self.calls.append(call)
        if self._script:
            step = self._script.pop(0)
            result = step(len(self.calls))
            if isinstance(result, Exception):
                call.future.set_exception(result)
            else:
                call.future.set_result(result)
        return call.future

    def resolve(self, value: Any, index: int = -1) -> None:
        self.calls[index].future.set_result(value)

    def fail(self, exc: Optional[BaseException] = None, index: int = -1) -> None:
        self.calls[index].future.set_exception(exc or OriginError("boom"))


def value_of(fut: Future) -> Any:
    assert fut.done(), "future unexpectedly pending"
    return fut.result(timeout=0)


def marker_of(fut: Future) -> StaleMarker:
    res = value_of(fut)
    assert isinstance(res, StaleMarker), f"expected stale marker, got {res!r}"
    return res
