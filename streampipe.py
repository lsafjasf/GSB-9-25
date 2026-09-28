"""streampipe: 有界缓冲 + 背压的多级流式数据处理管道（仅标准库）。

设计要点
========
- 每两个相邻阶段之间、以及生产者到首阶段、末阶段到消费者之间，
  各有一个容量为 ``capacity`` 的有界缓冲（``BoundedBuffer``）。
- 背压策略（``policy``）：
    * ``"block"``       缓冲满时 put 阻塞（可带 timeout / cancel_event）；
    * ``"drop_oldest"`` 缓冲满时丢弃最旧元素，put 永不阻塞；
    * ``"drop_newest"`` 缓冲满时丢弃新元素，put 返回 False。
- 错误传播：任一阶段抛出异常 -> 记录错误 -> 关闭全部缓冲 ->
  丢弃所有已缓冲数据（计入 dropped）-> 消费者迭代器原样抛出该异常
  （类型不变，调用方可直接 except 具体类型）。仅 abort() 中止路径抛
  PipelineError。已交付给消费者的结果保持有效；未交付的一律丢弃。
- 关闭语义：
    * ``close()``  优雅关闭：生产者再写入会抛 ClosedError；管道把已缓冲
      数据处理完后，消费者能读到全部剩余数据；重复调用安全（幂等）。
    * ``abort()``  立即终止（用于消费者异常等）：丢弃全部缓冲数据，
      生产者写入抛 ClosedError，消费者迭代抛 PipelineError。
    * 消费者中途放弃迭代（results() 生成器被 close，例如 break 后被
      回收）等价于 abort()：阻塞在满缓冲上的生产者/阶段线程全部解除退出。

内存上界推导
============
设阶段数 S，每缓冲容量 C，缓冲数 B = S + 1。
- 每个 BoundedBuffer 在同一条件变量下保证 len(items) <= C（丢弃策略同样不超 C）；
- 每个阶段工作线程在缓冲之外至多持有 1 个正在处理的元素；
- 因此管道内部驻留元素总数 <= B*C + S = (S+1)*C + S。
若单个元素大小 <= m 字节，则管道峰值内存 <= ((S+1)*C + S) * m + O(固定开销)。
测试中用全局在途计数器（tracker.peak）与每缓冲高水位（buffer.peak）验证该上界。
"""

from __future__ import annotations

import collections
import threading
import time

__all__ = [
    "Pipeline",
    "BoundedBuffer",
    "Stats",
    "SKIP",
    "ClosedError",
    "BackpressureTimeout",
    "PutCancelled",
    "PipelineError",
]


class ClosedError(Exception):
    """向已关闭的管道/缓冲写入。"""


class BackpressureTimeout(TimeoutError):
    """block 策略下 put 等待缓冲空位超时。"""


class PutCancelled(Exception):
    """block 策略下 put 在背压等待期间被 cancel_event 取消。"""


class PipelineError(Exception):
    """某一阶段失败或管道被中止，沿管道传播给消费者。"""


class _Sentinel:
    def __init__(self, name):
        self._name = name

    def __repr__(self):  # pragma: no cover
        return self._name


_END = _Sentinel("<END>")   # get() 在缓冲关闭且排空后返回
SKIP = _Sentinel("<SKIP>")  # 阶段函数返回 SKIP 表示过滤该元素（计入 dropped）


class _InflightTracker:
    """全局在途元素计数（只统计驻留在缓冲中的元素），用于证明内存上界。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def inc(self):
        with self._lock:
            self.current += 1
            if self.current > self.peak:
                self.peak = self.current

    def dec(self, n=1):
        with self._lock:
            self.current -= n


class BoundedBuffer:
    """单条件变量实现的有界缓冲，支持 block / drop_oldest / drop_newest。"""

    POLICIES = ("block", "drop_oldest", "drop_newest")

    def __init__(self, capacity, policy="block", tracker=None, name="buffer"):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if policy not in self.POLICIES:
            raise ValueError(f"unknown policy: {policy!r}")
        self.capacity = capacity
        self.policy = policy
        self.name = name
        self._items = collections.deque()
        self._closed = False
        self._cond = threading.Condition()
        self._tracker = tracker
        # 统计
        self.peak = 0        # 本缓冲高水位，恒 <= capacity
        self.enqueued = 0    # 成功入队次数
        self.dropped = 0     # 因丢弃策略/关闭丢弃的元素数

    def put(self, item, timeout=None, cancel_event=None):
        """写入元素。返回 True 表示入队；drop_newest 下被丢弃返回 False。

        block 策略下缓冲满会阻塞，直到有空位 / 超时 / 被取消 / 缓冲关闭。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                if self._closed:
                    raise ClosedError(f"{self.name}: put on closed buffer")
                if len(self._items) < self.capacity:
                    self._push(item)
                    return True
                if self.policy == "drop_oldest":
                    self._items.popleft()
                    self.dropped += 1
                    if self._tracker:
                        self._tracker.dec()
                    self._push(item)
                    return True
                if self.policy == "drop_newest":
                    self.dropped += 1
                    self._cond.notify_all()
                    return False
                # block 策略：等待空位
                if cancel_event is not None and cancel_event.is_set():
                    raise PutCancelled(f"{self.name}: put cancelled under backpressure")
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BackpressureTimeout(
                            f"{self.name}: put timed out after {timeout}s (capacity={self.capacity})"
                        )
                    self._cond.wait(min(remaining, 0.05))
                elif cancel_event is not None:
                    self._cond.wait(0.05)  # 轮询取消事件
                else:
                    self._cond.wait()

    def _push(self, item):
        self._items.append(item)
        self.enqueued += 1
        if len(self._items) > self.peak:
            self.peak = len(self._items)
        if self._tracker:
            self._tracker.inc()
        self._cond.notify_all()

    def get(self):
        """取出一个元素；缓冲关闭且已排空时返回 _END。"""
        with self._cond:
            while not self._items:
                if self._closed:
                    return _END
                self._cond.wait()
            item = self._items.popleft()
            if self._tracker:
                self._tracker.dec()
            self._cond.notify_all()
            return item

    def close(self):
        """幂等关闭。关闭后 put 抛 ClosedError；已缓冲数据仍可被 get 取走。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def discard_remaining(self):
        """丢弃全部已缓冲数据（用于失败/中止路径），返回丢弃数量。"""
        with self._cond:
            n = len(self._items)
            if n:
                self._items.clear()
                self.dropped += n
                if self._tracker:
                    self._tracker.dec(n)
            self._closed = True
            self._cond.notify_all()
            return n

    @property
    def closed(self):
        with self._cond:
            return self._closed

    def __len__(self):
        with self._cond:
            return len(self._items)


class Stats:
    """管道运行统计。"""

    def __init__(self, written, processed, dropped, failed, peak_buffered,
                 capacity_bound, per_buffer_peak, aborted):
        self.written = written            # 生产者成功写入管道的元素数
        self.processed = processed        # 各阶段成功处理并交付下游的元素数（累计）
        self.dropped = dropped            # 丢弃总数（策略丢弃 + 失败/中止丢弃 + SKIP 过滤）
        self.failed = failed              # 阶段失败次数
        self.peak_buffered = peak_buffered      # 全局在途（驻留缓冲）峰值
        self.capacity_bound = capacity_bound    # 理论缓冲上界 B*C
        self.per_buffer_peak = tuple(per_buffer_peak)
        self.aborted = aborted

    def as_dict(self):
        return {
            "written": self.written,
            "processed": self.processed,
            "dropped": self.dropped,
            "failed": self.failed,
            "peak_buffered": self.peak_buffered,
            "capacity_bound": self.capacity_bound,
            "per_buffer_peak": self.per_buffer_peak,
            "aborted": self.aborted,
        }

    def __str__(self):
        return (
            "Stats("
            f"written={self.written}, processed={self.processed}, "
            f"dropped={self.dropped}, failed={self.failed}, "
            f"peak_buffered={self.peak_buffered} (bound={self.capacity_bound}), "
            f"per_buffer_peak={self.per_buffer_peak}, aborted={self.aborted})"
        )


class Pipeline:
    """多级阶段串联的流式管道。

    用法::

        pipe = Pipeline([stage1, stage2], capacity=8, policy="block")
        pipe.put(item)                 # 生产者（可在任意线程）
        pipe.close()                   # 关闭输入，幂等
        for result in pipe.results():  # 消费者迭代，读到剩余全部数据
            ...
        print(pipe.stats())
    """

    def __init__(self, stages, capacity=16, policy="block", name="pipeline"):
        if not stages:
            raise ValueError("at least one stage is required")
        self.name = name
        self._stages = list(stages)
        self._tracker = _InflightTracker()
        # 缓冲数 B = S + 1：输入缓冲 + 阶段间缓冲 + 输出缓冲
        self._buffers = [
            BoundedBuffer(capacity, policy, self._tracker, name=f"{name}/buf{i}")
            for i in range(len(self._stages) + 1)
        ]
        self._lock = threading.Lock()
        self._closed = False
        self._error = None       # 阶段异常
        self._aborted = False
        self._written = 0
        self._processed = [0] * len(self._stages)
        self._filtered = 0       # 阶段返回 SKIP 过滤掉的元素
        self._threads = [
            threading.Thread(
                target=self._run_stage, args=(i, fn),
                name=f"{name}-stage{i}", daemon=True,
            )
            for i, fn in enumerate(self._stages)
        ]
        for t in self._threads:
            t.start()

    # ---------------------------------------------------------------- 生产者侧

    def put(self, item, timeout=None, cancel_event=None):
        """写入一个元素。返回 True 表示接受；drop_newest 被丢弃返回 False。

        - 管道已关闭/失败/中止：抛 ClosedError；
        - block 策略满缓冲：阻塞，可用 timeout（抛 BackpressureTimeout）
          或 cancel_event（抛 PutCancelled）退出。
        """
        with self._lock:
            if self._closed:
                raise ClosedError(f"{self.name}: put on closed pipeline")
        accepted = self._buffers[0].put(item, timeout=timeout, cancel_event=cancel_event)
        if accepted:
            with self._lock:
                self._written += 1
        return accepted

    def close(self):
        """优雅关闭输入（幂等）。已缓冲数据会被处理完，消费者可读到剩余全部数据。"""
        with self._lock:
            if self._closed:
                return False
            self._closed = True
        self._buffers[0].close()
        return True

    def abort(self):
        """立即终止整条管道（幂等）：丢弃全部缓冲数据，写入抛 ClosedError。

        已处于失败状态时无需（也不会）重复清理：缓冲已在失败路径关闭。
        """
        with self._lock:
            if self._aborted:
                return False
            self._aborted = True
            self._closed = True
        self._teardown_buffers()
        return True

    # ---------------------------------------------------------------- 消费者侧

    def results(self):
        """迭代输出。

        - 优雅关闭后能读到剩余全部数据；
        - 阶段失败时原样抛出阶段抛出的异常（类型不变，可直接 except）；
        - abort() 中止时抛 PipelineError；
        - 迭代被中途放弃（生成器被 close，如 break 后回收）时自动中止
          管道，解除所有阻塞在背压上的生产者与阶段线程。
        """
        out = self._buffers[-1]
        finished = False
        try:
            while True:
                item = out.get()
                if item is _END:
                    break
                yield item
            finished = True
            if self._error is not None:
                raise self._error
            if self._aborted:
                raise PipelineError(f"{self.name}: pipeline aborted")
        finally:
            if not finished:
                # 消费者中途放弃迭代（GeneratorExit / 被回收）：
                # 立即中止，防止上游线程永久阻塞在满缓冲的 put 上。
                self.abort()

    def __iter__(self):
        return self.results()

    # ---------------------------------------------------------------- 生命周期

    def wait(self, timeout=None):
        """等待所有阶段线程结束。返回是否全部结束。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in self._threads:
            remain = None if deadline is None else max(0.0, deadline - time.monotonic())
            t.join(remain)
        return not any(t.is_alive() for t in self._threads)

    def stats(self):
        with self._lock:
            processed = sum(self._processed)
            written = self._written
            failed = 1 if self._error is not None else 0
            dropped = self._filtered
            aborted = self._aborted
        dropped += sum(b.dropped for b in self._buffers)
        bound = len(self._buffers) * self._buffers[0].capacity
        return Stats(
            written=written,
            processed=processed,
            dropped=dropped,
            failed=failed,
            peak_buffered=self._tracker.peak,
            capacity_bound=bound,
            per_buffer_peak=[b.peak for b in self._buffers],
            aborted=aborted,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self.abort()  # 消费者异常 -> 立即终止
        self.wait(timeout=5)
        return False

    # ---------------------------------------------------------------- 内部

    def _run_stage(self, idx, fn):
        in_buf = self._buffers[idx]
        out_buf = self._buffers[idx + 1]
        try:
            while True:
                item = in_buf.get()
                if item is _END:
                    out_buf.close()  # 把“流结束”逐级传播到下游
                    return
                result = fn(item)
                if result is SKIP:
                    with self._lock:
                        self._filtered += 1
                    continue
                try:
                    out_buf.put(result)
                except ClosedError:
                    return  # 下游已关闭（失败/中止），本阶段退出
                with self._lock:
                    self._processed[idx] += 1
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc):
        with self._lock:
            if self._error is not None or self._aborted:
                return
            self._error = exc
            self._closed = True
        self._teardown_buffers()

    def _teardown_buffers(self):
        """失败/中止路径：关闭所有缓冲并丢弃其中的数据（计入 dropped）。"""
        for buf in self._buffers:
            buf.discard_remaining()


# ---------------------------------------------------------------- 演示

if __name__ == "__main__":
    import random

    def stage_parse(x):
        return x * 2

    def stage_filter(x):
        return SKIP if x % 5 == 0 else x

    def stage_format(x):
        return f"item-{x}"

    pipe = Pipeline([stage_parse, stage_filter, stage_format], capacity=8, policy="block")

    N = 1000

    def producer():
        for i in range(N):
            pipe.put(i)
        pipe.close()

    prod = threading.Thread(target=producer, name="demo-producer")
    prod.start()

    received = 0
    for _ in pipe.results():
        received += 1
        if random.random() < 0.01:
            time.sleep(0.001)  # 模拟慢消费者
    prod.join()
    pipe.wait()

    print(f"consumed {received}/{N} items")
    print(pipe.stats())
