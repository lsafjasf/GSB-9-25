"""修复后的线程安全对象池（仅标准库）。

语义约定
--------
获取超时：acquire(timeout) 在超时后抛出 PoolTimeout，
    绝不返回 None，也绝不返回已销毁的对象；timeout=None 表示无限等待。
池关闭：close() 销毁全部空闲对象、唤醒所有等待者（抛 PoolClosed）；
    在途（已借出未归还）对象在归还时被销毁而不再入池；
    关闭后再 acquire 立即抛 PoolClosed；close() 幂等。
归还校验：release 只接受 acquire 签发的借出凭据（Lease）。凭据按次签发、
    归还即失效；同一对象被再次借出时签发的是新凭据，旧凭据的“陈旧归还”
    会被 ReleaseError 拒绝，保证任一时刻同一对象只被一个调用方持有。
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
    """非法归还：凭据不属于本池、已失效（重复归还）或已陈旧（对象被再次借出）。"""


PoolStats = collections.namedtuple(
    "PoolStats", ["idle", "in_use", "total", "peak", "max_size", "closed"]
)


class Lease:
    """借出凭据：每次成功 acquire 签发一个全新凭据，release 必须出示。

    凭据按对象身份（identity）唯一，且仅在本次借出期间有效：
    归还后立即失效；对象被再次借出时签发的是另一个凭据。
    因此持有旧凭据的调用方无法把他人正在使用的对象“还”回池里，
    从机制上杜绝陈旧二次归还导致的双重持有。
    """

    __slots__ = ("obj",)

    def __init__(self, obj):
        self.obj = obj

    def __repr__(self):
        return "<Lease obj=%r>" % (self.obj,)


def _default_destructor(obj):
    close = getattr(obj, "close", None)
    if callable(close):
        close()


class ObjectPool:
    """限制最大对象数、复用开销较大对象的线程安全池。

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
        """借出一个对象，返回其借出凭据（Lease，用 lease.obj 取对象）。

        超时抛 PoolTimeout；池关闭抛 PoolClosed。
        """
        with self._cond:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                if self._closed:
                    raise PoolClosed("pool is closed")
                if self._idle:
                    obj = self._idle.popleft()
                    lease = Lease(obj)
                    self._in_use.add(lease)
                    return lease
                if self._total < self._max_size:
                    self._total += 1
                    self._peak = max(self._peak, self._total)
                    try:
                        obj = self._factory()
                    except BaseException:
                        self._total -= 1  # 工厂失败：回滚计数，保持守恒
                        self._cond.notify()
                        raise
                    lease = Lease(obj)
                    self._in_use.add(lease)
                    return lease
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
        lease = self.acquire(timeout=timeout)
        try:
            yield lease.obj
        finally:
            self.release(lease)

    # ------------------------------------------------------------------ 归还
    def release(self, lease):
        """归还对象。只接受当前在途的借出凭据（Lease）。

        重复归还、陈旧凭据（归还后对象已被他人再次借出）、
        外来对象或伪造凭据均抛 ReleaseError。
        """
        with self._cond:
            if lease not in self._in_use:
                raise ReleaseError(
                    "invalid or stale checkout lease (double release?)"
                )
            self._in_use.discard(lease)
            obj = lease.obj
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
            in_use_ids = {id(lease.obj) for lease in self._in_use}
            assert len(in_use_ids) == len(self._in_use), (
                "同一对象不得同时拥有多个在途凭据"
            )
            idle_ids = {id(obj) for obj in self._idle}
            assert not idle_ids & in_use_ids, "idle/in_use 必须互斥"
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
