"""In-memory bidirectional channel simulator (stdlib only).

Injects latency, jitter (reordering), packet loss and replay (duplicates).
Supports disconnect/reconnect: pending in-flight messages are dropped on
disconnect and hooks notify both ends.
"""

import heapq
import itertools
import random
import threading
import time


class ChannelDownError(Exception):
    """Raised when sending on a disconnected/closed channel."""


class SimChannel:
    def __init__(self, latency=0.001, jitter=0.0, loss_rate=0.0,
                 replay_rate=0.0, seed=None):
        if not 0.0 <= loss_rate <= 1.0 or not 0.0 <= replay_rate <= 1.0:
            raise ValueError("loss_rate/replay_rate must be in [0, 1]")
        self.latency = latency
        self.jitter = jitter
        self.loss_rate = loss_rate
        self.replay_rate = replay_rate
        self._rng = random.Random(seed)
        self._cv = threading.Condition()
        self._heap = []  # (deliver_at, seq, fn, msg)
        self._seq = itertools.count()
        self._connected = True
        self._closed = False
        self._peer = None            # server-side handler: fn(msg) -> response | None
        self._receiver = None        # client-side receiver: fn(msg)
        self._disconnect_hook = None
        self._reconnect_hook = None
        # counters
        self.sent = 0
        self.dropped = 0
        self.delivered = 0
        self.replayed = 0
        self._thread = threading.Thread(target=self._run, name="SimChannel",
                                        daemon=True)
        self._thread.start()

    # -- wiring ---------------------------------------------------------
    def set_peer(self, handler):
        self._peer = handler

    def set_receiver(self, fn):
        self._receiver = fn

    def set_disconnect_hook(self, fn):
        self._disconnect_hook = fn

    def set_reconnect_hook(self, fn):
        self._reconnect_hook = fn

    @property
    def connected(self):
        with self._cv:
            return self._connected

    # -- client -> peer -------------------------------------------------
    def send(self, msg):
        with self._cv:
            if self._closed:
                raise ChannelDownError("channel closed")
            if not self._connected:
                raise ChannelDownError("channel disconnected")
            self.sent += 1
            if self._rng.random() < self.loss_rate:
                self.dropped += 1
                return
            self._schedule_locked(self._deliver_to_peer, msg)

    # -- connection control ---------------------------------------------
    def disconnect(self):
        with self._cv:
            if not self._connected:
                return
            self._connected = False
            self._heap.clear()  # in-flight messages are lost
        if self._disconnect_hook:
            self._disconnect_hook()

    def reconnect(self):
        with self._cv:
            if self._connected:
                return
            self._connected = True
        if self._reconnect_hook:
            self._reconnect_hook()

    def close(self):
        with self._cv:
            self._closed = True
            self._heap.clear()
            self._cv.notify_all()
        self._thread.join(timeout=2)

    # -- internals --------------------------------------------------------
    def _schedule_locked(self, fn, msg):
        delay = self.latency
        if self.jitter:
            delay += self._rng.uniform(0.0, self.jitter)
        heapq.heappush(self._heap,
                       (time.monotonic() + delay, next(self._seq), fn, msg))
        self._cv.notify_all()

    def _deliver_to_peer(self, msg):
        peer = self._peer
        if peer is None:
            return
        response = peer(msg)
        if response is None:
            return
        with self._cv:
            if self._rng.random() < self.loss_rate:
                self.dropped += 1
                return
            self._schedule_locked(self._deliver_to_client, response)
            if self._rng.random() < self.replay_rate:
                self.replayed += 1
                self._schedule_locked(self._deliver_to_client, response)

    def _deliver_to_client(self, msg):
        receiver = self._receiver
        if receiver is None:
            return
        with self._cv:
            self.delivered += 1
        receiver(msg)

    def _run(self):
        while True:
            with self._cv:
                while not self._closed and not self._heap:
                    self._cv.wait()
                if self._closed:
                    return
                due, _, fn, msg = self._heap[0]
                wait = due - time.monotonic()
                if wait > 0:
                    self._cv.wait(timeout=wait)
                    continue
                heapq.heappop(self._heap)
            try:
                fn(msg)
            except Exception:
                pass  # a broken receiver must not kill the channel
