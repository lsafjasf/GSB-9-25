"""修复后的线程安全对象池（仅标准库）。

语义约定
--------
获取超时：acquire(timeout) 在超时后抛出 PoolTimeout，
    绝不返回 None，也绝不返回已销毁的对象；timeout=None 表示无限等待。
池关闭：close() 销毁全部空闲对象、唤醒所有等待者（抛 PoolClosed）；
    在途（已借出未归还）对象在归还时被销毁而不再入池；
    关闭后再 acquire 立即抛 PoolClosed；close() 幂等。
归还校验：重复归还或归还外来对象抛 ReleaseError，
    保证任一时刻同一对象只被一个调用方持有。
计数：stats() 返回 (idle, in_use, total, peak)，任意时刻满足
    total == idle + in_use、0 <= total <= max_size、
    total <= peak <= max_size；check_invariants() 可断言这些不变量。
异常安全：推荐 with pool.item() as conn: ...（contextmanager + finally），
    或手写 try/finally: pool.release(conn)，保证异常路径也归还。
"""

import collections
import contextlib
import threading
import time


class PoolClosed(Exception):
    """池已关闭：关闭后获取，或等待期间池被关闭。"""


class PoolTimeout(TimeoutError):
    """在指定超时时间内未能获取到对象。"""


class ReleaseError(Exception):
    """非法归还：对象不属于本池，或同一对象被重复归还。"""


PoolStats = collections.namedtuple(
    "PoolStats", ["idle", "in_use", "total", "peak", "max_size", "closed"]
)


def _default_destructor(obj):
    close = getattr(obj, "close", None)
    if callable(close):
        close()


class ObjectPool:
    """限制最大对象数、复用开销较大对象的线程安全池。

    对象必须可哈希（普通对象默认即可），因为内部用集合跟踪在途对象。
    factory 在池内部锁下调用以保证计数在任意时刻都与实际一致，
    因此 factory 应当尽量快；慢工厂只会串行化“新建”，不影响已有对象的并发借还。
    """

    def __init__(self, factory, max_size, destructor=None):
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._factory = factory
        self._destructor = destructor or _default_destructor
        self._max_size = max_size
        self._idle = collections.deque()
        self._in_use = set()
        self._total = 0
        self._peak = 0
        self._closed = False
        self._cond = threading.Condition()

    # ------------------------------------------------------------------ 获取
    def acquire(self, timeout=None):
        """获取一个对象。超时抛 PoolTimeout；池关闭抛 PoolClosed。"""
        with self._cond:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                if self._closed:
                    raise PoolClosed("pool is closed")
                if self._idle:
                    obj = self._idle.popleft()
                    self._in_use.add(obj)
                    return obj
                if self._total < self._max_size:
                    self._total += 1
                    self._peak = max(self._peak, self._total)
                    try:
                        obj = self._factory()
                    except BaseException:
                        self._total -= 1  # 工厂失败：回滚计数，保持守恒
                        self._cond.notify()
                        raise
                    self._in_use.add(obj)
                    return obj
                # 池已满：等待归还或关闭
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolTimeout(
                        "no object available within %r seconds" % (timeout,)
                    )
                self._cond.wait(remaining)

    @contextlib.contextmanager
    def item(self, timeout=None):
        """上下文管理器：异常路径也通过 finally 保证归还。"""
        obj = self.acquire(timeout=timeout)
        try:
            yield obj
        finally:
            self.release(obj)

    # ------------------------------------------------------------------ 归还
    def release(self, obj):
        """归还对象。重复归还/外来对象抛 ReleaseError。"""
        with self._cond:
            if obj not in self._in_use:
                raise ReleaseError(
                    "object was not checked out from this pool (double release?)"
                )
            self._in_use.discard(obj)
            if self._closed:
                # 池已关闭：在途对象归还时销毁，不再入池
                self._total -= 1
                self._destroy(obj)
            else:
                self._idle.append(obj)
            self._cond.notify()

    # ------------------------------------------------------------------ 关闭
    def close(self):
        """关闭池：销毁空闲对象、唤醒等待者；在途对象归还时销毁。幂等。"""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            destroyed = list(self._idle)
            self._idle.clear()
            self._total -= len(destroyed)
            for obj in destroyed:
                self._destroy(obj)
            self._cond.notify_all()  # 唤醒所有等待者，它们将抛 PoolClosed

    # ------------------------------------------------------------------ 观测
    def stats(self):
        """返回计数快照；快照在锁内生成，与任一时刻的实际状态一致。"""
        with self._cond:
            return PoolStats(
                idle=len(self._idle),
                in_use=len(self._in_use),
                total=self._total,
                peak=self._peak,
                max_size=self._max_size,
                closed=self._closed,
            )

    def check_invariants(self):
        """断言计数与结构不变量；任何时刻调用都必须成立。"""
        with self._cond:
            assert self._total == len(self._idle) + len(self._in_use), (
                "total(%d) != idle(%d) + in_use(%d)"
                % (self._total, len(self._idle), len(self._in_use))
            )
            assert 0 <= self._total <= self._max_size
            assert self._total <= self._peak <= self._max_size
            assert not set(self._idle) & self._in_use, "idle/in_use 必须互斥"
            assert not (self._closed and self._idle), "closed pool must be empty"
            return True

    # ------------------------------------------------------------------ 内部
    def _destroy(self, obj):
        try:
            self._destructor(obj)
        except Exception:
            pass  # 销毁失败不影响池状态一致性

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False
