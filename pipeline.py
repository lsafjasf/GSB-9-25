"""流式数据处理管道：有界缓冲 + 背压 + 错误传播 + 完整关闭语义。

仅使用 Python 标准库。线程模型：每个阶段（stage）一个工作线程，
阶段之间用有界缓冲（BoundedBuffer）连接；生产者调用 ``put`` 写入，
消费者通过迭代管道读取结果。

内存上界推导
------------
设管道有 k 个阶段，输入缓冲容量为 C_0，第 i 个阶段的输出缓冲容量为 C_i。
任意时刻驻留在内存中的元素数量 M 满足：

    M <= C_0 + C_1 + ... + C_k      （所有缓冲队列中的元素之和）
       + (k + 1)                    （每个阶段线程手中正在处理的 1 个元素
                                      + 消费者手中正在消费的 1 个）

理由：
- BoundedBuffer.put 严格保证队列长度永不超过 capacity：
  BLOCK 策略下满则阻塞生产者；DROP_NEWEST / DROP_OLDEST 策略下满则丢弃，
  队列长度始终 <= capacity。
- 每个阶段线程同一时刻最多持有 1 个已取出、尚未写入下游的元素。

因此峰值内存只取决于配置容量与阶段数，与生产速度、数据总量无关。
测试中用监视线程采样 ``sum(len(buf))`` 验证该上界。

错误语义
--------
任一阶段抛错 -> Pipeline._record_failure 记录首个异常并 abort 所有缓冲：
- 已缓冲但未处理的数据一律丢弃，并计入统计的 dropped（不静默吞掉）；
- 阻塞中的生产者 put 立即以该异常唤醒；
- 消费者迭代时收到该异常（原始异常类型原样抛出）。

关闭语义
--------
- close() 后 put 抛 ClosedError；已写入的数据会被各阶段处理完，
  消费者能读到全部剩余结果后再结束；close 幂等。
- abort() 后 put 抛错、缓冲数据丢弃、消费者收到异常；abort 幂等。
- 消费者中途放弃迭代（生成器被关闭）会自动 abort 整条管道，
  阻塞中的生产者会被唤醒并收到异常，不会挂死。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Iterator, List, Optional


class PipelineError(Exception):
    """管道级错误的基类。"""


class ClosedError(PipelineError):
    """向已关闭的管道写入。"""


class BackpressureTimeout(PipelineError):
    """BLOCK 策略下等待背压超时。"""


class DropPolicy(Enum):
    """缓冲满时的背压策略。"""

    BLOCK = "block"                # 阻塞生产者直到有空间（或关闭/中止）
    DROP_NEWEST = "drop_newest"    # 丢弃新写入的元素
    DROP_OLDEST = "drop_oldest"    # 挤出最旧的缓冲元素


_END = object()   # 关闭后传递给下游的结束哨兵
SKIP = object()   # 阶段函数返回它表示过滤掉该元素


@dataclass
class BufferStats:
    written: int = 0    # 成功入队的元素数
    dropped: int = 0    # 被丢弃的元素数（策略丢弃 + abort 时清空）
    peak: int = 0       # 队列长度历史峰值（内存上界证据）


@dataclass
class StageStats:
    processed: int = 0  # 成功处理的元素数
    failed: int = 0     # 首个引发管道失败的异常计 1


class BoundedBuffer:
    """有界缓冲：满时按策略阻塞或丢弃；支持关闭与中止。"""

    def __init__(self, capacity: int, policy: DropPolicy = DropPolicy.BLOCK,
                 name: str = "") -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.policy = policy
        self.name = name
        self.stats = BufferStats()
        self._items: Deque[Any] = deque()
        self._cond = threading.Condition()
        self._closed = False
        self._aborted = False
        self._error: Optional[BaseException] = None

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)

    def put(self, item: Any, timeout: Optional[float] = None) -> bool:
        """写入一个元素。返回是否真正入队（DROP_NEWEST 满时返回 False）。

        可能抛出：ClosedError（已关闭）、BackpressureTimeout（阻塞超时）、
        或 abort 时记录的原始异常。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                if self._aborted:
                    raise self._error or PipelineError("pipeline aborted")
                if self._closed:
                    raise ClosedError("buffer is closed")
                if len(self._items) < self.capacity:
                    break  # 有空间，可以写入
                if self.policy is DropPolicy.DROP_NEWEST:
                    self.stats.dropped += 1
                    return False
                if self.policy is DropPolicy.DROP_OLDEST:
                    self._items.popleft()
                    self.stats.dropped += 1
                    break
                # BLOCK：等待消费者腾出空间
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise BackpressureTimeout("put timed out under backpressure")
                self._cond.wait(remaining)
            self._items.append(item)
            self.stats.written += 1
            if len(self._items) > self.stats.peak:
                self.stats.peak = len(self._items)
            self._cond.notify_all()
            return True

    def get(self) -> Any:
        """取出一个元素；关闭且排空后返回 _END；abort 时抛错。"""
        with self._cond:
            while not self._items:
                if self._aborted:
                    raise self._error or PipelineError("pipeline aborted")
                if self._closed:
                    return _END
                self._cond.wait()
            item = self._items.popleft()
            self._cond.notify_all()
            return item

    def close(self) -> None:
        """关闭写入端：put 失败，缓冲中已有数据仍可被消费。幂等。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def abort(self, exc: BaseException) -> None:
        """中止：丢弃全部缓冲数据（计入 dropped），唤醒所有等待者。幂等。"""
        with self._cond:
            if self._aborted:
                return
            self._aborted = True
            self._error = exc
            self.stats.dropped += len(self._items)
            self._items.clear()
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    @property
    def aborted(self) -> bool:
        with self._cond:
            return self._aborted


@dataclass
class _Stage:
    fn: Callable[[Any], Any]
    name: str
    stats: StageStats = field(default_factory=StageStats)


@dataclass
class PipelineStats:
    """整条管道的汇总统计。"""

    written: int = 0        # 写入管道的元素数
    processed: int = 0      # 各阶段处理次数之和
    dropped: int = 0        # 各缓冲丢弃数之和
    failed: int = 0         # 引发失败的阶段数（0 或 1）
    peak_buffered: int = 0  # 所有缓冲峰值之和（内存上界证据）
    buffers: List[BufferStats] = field(default_factory=list)
    stages: List[StageStats] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"written={self.written} processed={self.processed} "
            f"dropped={self.dropped} failed={self.failed} "
            f"peak_buffered={self.peak_buffered}"
        )


class Pipeline:
    """多级流式管道。

    用法::

        pipe = Pipeline(capacity=8)
        pipe.add_stage(parse).add_stage(transform)
        pipe.start()
        pipe.put(item)          # 背压在此生效
        pipe.close()            # 关闭写入端
        for result in pipe:     # 消费者读到全部剩余结果
            ...
        print(pipe.stats())
    """

    def __init__(self, capacity: int = 16,
                 policy: DropPolicy = DropPolicy.BLOCK) -> None:
        self._capacity = capacity
        self._policy = policy
        self._stages: List[_Stage] = []
        self._buffers: List[BoundedBuffer] = []
        self._threads: List[threading.Thread] = []
        self._started = False
        self._lock = threading.Lock()
        self._error: Optional[BaseException] = None

    # ---------------------------------------------------------------- 构建

    def add_stage(self, fn: Callable[[Any], Any],
                  name: Optional[str] = None,
                  capacity: Optional[int] = None,
                  policy: Optional[DropPolicy] = None) -> "Pipeline":
        """追加一个处理阶段。fn 返回处理结果；返回 SKIP 表示过滤。"""
        if self._started:
            raise PipelineError("cannot add stages after start")
        self._stages.append(_Stage(fn, name or getattr(fn, "__name__", "stage")))
        self._buffers.append(BoundedBuffer(
            capacity or self._capacity,
            policy or self._policy,
            name=f"out[{len(self._stages) - 1}]",
        ))
        return self

    # ---------------------------------------------------------------- 运行

    def start(self) -> "Pipeline":
        """启动所有阶段线程。"""
        if not self._stages:
            raise PipelineError("pipeline needs at least one stage")
        if self._started:
            return self
        self._started = True
        # 输入缓冲（生产者写入）插到最前面
        self._buffers.insert(0, BoundedBuffer(self._capacity, self._policy, "in"))
        for i, stage in enumerate(self._stages):
            t = threading.Thread(
                target=self._stage_loop,
                args=(stage, self._buffers[i], self._buffers[i + 1]),
                name=f"pipeline-stage-{i}-{stage.name}",
                daemon=True,
            )
            self._threads.append(t)
            t.start()
        return self

    def _stage_loop(self, stage: _Stage, in_buf: BoundedBuffer,
                    out_buf: BoundedBuffer) -> None:
        try:
            while True:
                item = in_buf.get()
                if item is _END:
                    break
                result = stage.fn(item)
                stage.stats.processed += 1
                if result is SKIP:
                    continue
                out_buf.put(result)
        except BaseException as exc:  # 必须沿管道传播一切异常
            if self._record_failure(exc):
                stage.stats.failed += 1
        finally:
            # 正常结束：关闭下游，让消费者能读到全部剩余数据。
            # 异常结束：_record_failure 已 abort 所有缓冲，close 是幂等 no-op。
            out_buf.close()

    def _record_failure(self, exc: BaseException) -> bool:
        """记录首个异常并 abort 所有缓冲。返回是否为首个失败。"""
        with self._lock:
            if self._error is not None:
                return False
            self._error = exc
        for buf in self._buffers:
            buf.abort(exc)
        return True

    # ---------------------------------------------------------------- 生产

    def put(self, item: Any, timeout: Optional[float] = None) -> bool:
        """写入一个元素，背压策略在此生效。"""
        self._require_started()
        return self._buffers[0].put(item, timeout=timeout)

    def close(self) -> None:
        """关闭写入端：之后 put 抛 ClosedError；已写入数据会被处理完。幂等。"""
        self._require_started()
        self._buffers[0].close()

    def abort(self, exc: Optional[BaseException] = None) -> None:
        """立即中止整条管道：丢弃缓冲数据，唤醒所有阻塞方。幂等。"""
        self._require_started()
        self._record_failure(exc or PipelineError("pipeline aborted"))

    # ---------------------------------------------------------------- 消费

    def __iter__(self) -> Iterator[Any]:
        """消费输出。阶段出错时抛出原始异常；消费者中途放弃会自动 abort。"""
        self._require_started()
        return self._consume()

    def _consume(self) -> Iterator[Any]:
        out = self._buffers[-1]
        exhausted = False
        try:
            while True:
                item = out.get()
                if item is _END:
                    exhausted = True
                    break
                yield item
        except GeneratorExit:
            # 消费者异常退出 / 放弃迭代：中止管道，唤醒阻塞的生产者
            self._record_failure(PipelineError("consumer stopped iterating"))
            raise
        finally:
            self.join()
            # 正常读完后，若期间有阶段失败（残留错误），也不能静默吞掉
            if exhausted and self._error is not None:
                raise self._error

    def join(self, timeout: Optional[float] = None) -> bool:
        """等待所有阶段线程结束。返回是否全部结束。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in self._threads:
            remaining = None if deadline is None else max(
                0.0, deadline - time.monotonic())
            t.join(remaining)
        return all(not t.is_alive() for t in self._threads)

    # ---------------------------------------------------------------- 统计

    def stats(self) -> PipelineStats:
        """汇总统计：写入、处理、丢弃、失败、缓冲峰值。"""
        s = PipelineStats()
        s.buffers = [b.stats for b in self._buffers]
        s.stages = [st.stats for st in self._stages]
        s.written = self._buffers[0].stats.written if self._buffers else 0
        s.processed = sum(st.stats.processed for st in self._stages)
        s.dropped = sum(b.stats.dropped for b in self._buffers)
        s.failed = sum(st.stats.failed for st in self._stages)
        s.peak_buffered = sum(b.stats.peak for b in self._buffers)
        return s

    @property
    def buffers(self) -> List[BoundedBuffer]:
        return list(self._buffers)

    @property
    def error(self) -> Optional[BaseException]:
        return self._error

    def _require_started(self) -> None:
        if not self._started:
            raise PipelineError("pipeline not started")


# -------------------------------------------------------------------- demo

def _demo() -> None:
    """演示：快生产者 + 慢消费者 + 背压，结束打印统计。"""
    capacity = 8
    pipe = Pipeline(capacity=capacity)
    pipe.add_stage(lambda x: x + 1, name="incr")
    pipe.add_stage(lambda x: x * 2, name="double")
    pipe.add_stage(lambda x: SKIP if x % 7 == 0 else x, name="filter7")
    pipe.start()

    total = 10_000

    def produce() -> None:
        for i in range(total):
            pipe.put(i)
        pipe.close()

    producer = threading.Thread(target=produce, name="demo-producer")
    producer.start()

    received = 0
    for _ in pipe:  # 慢消费者
        received += 1
        if received % 500 == 0:
            time.sleep(0.001)
    producer.join()

    stats = pipe.stats()
    print(f"consumed={received} (written={total}, filtered by stage)")
    print(f"stats: {stats}")
    bound = sum(b.capacity for b in pipe.buffers) + len(pipe.buffers)
    print(f"memory bound: peak_buffered={stats.peak_buffered} "
          f"<= sum(capacities)+stages+1 = {bound}")


if __name__ == "__main__":
    _demo()
