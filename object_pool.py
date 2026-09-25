"""修复后的线程安全对象池（仅标准库）。

语义约定（明确行为）：
- 获取超时：acquire(timeout=...) 在超时内拿不到对象时抛出 AcquireTimeout，
  绝不返回 None 或已销毁对象。
- 池关闭：close() 立即销毁所有空闲对象，并唤醒全部等待者
  （等待者收到 PoolClosed）；在途对象归还被销毁而非回池。
- 关闭后获取：acquire() / item() 抛出 PoolClosed。
- 重复归还 / 归还外来对象：release() 抛出 ValueError。
- 计数：idle_count / in_use_count / total_count 直接由内部集合推导，
  任何时刻与实际对象数一致；peak_count 记录历史峰值。
"""

import threading
import time
from contextlib import contextmanager


class PoolClosed(Exception):
    """池已关闭：关闭后获取、或等待中被 close 唤醒时抛出。"""


class AcquireTimeout(Exception):
    """在指定超时时间内未能获取到对象。"""


def _default_destroy(obj):
    close = getattr(obj, "close", None)
    if callable(close):
        close()


class ObjectPool:
    def __init__(self, factory, max_size, destroy=None):
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._factory = factory
        self._max_size = max_size
        self._destroy = destroy or _default_destroy
        self._cond = threading.Condition()
        self._idle = []          # 空闲对象（list，元素唯一）
        self._in_use = set()     # 在用对象（set，与 _idle 互斥）
        self._peak = 0
        self._closed = False

    # ---------------- 获取 / 归还 ----------------

    def acquire(self, timeout=None):
        """获取一个对象。timeout 为 None 表示无限等待。

        超时抛出 AcquireTimeout；池已关闭抛出 PoolClosed。
        返回的对象一定处于"在用"集合中，且未被销毁。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                if self._closed:
                    raise PoolClosed("pool is closed")
                if self._idle:
                    obj = self._idle.pop()
                    self._in_use.add(obj)
                    return obj
                if self._total_locked() < self._max_size:
                    # 在上限内创建新对象（持锁创建，保证计数与状态原子一致）
                    obj = self._factory()
                    self._in_use.add(obj)
                    self._peak = max(self._peak, self._total_locked())
                    return obj
                if timeout is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AcquireTimeout(
                            "no object available within %.3fs" % timeout)
                    self._cond.wait(remaining)
                else:
                    self._cond.wait()

    def release(self, obj):
        """归还对象。重复归还或归还外来对象抛出 ValueError。

        池已关闭时，归还的在途对象会被销毁而不是回池。
        """
        destroy = False
        with self._cond:
            if obj not in self._in_use:
                raise ValueError(
                    "object was not acquired from this pool or already released")
            self._in_use.discard(obj)
            if self._closed:
                destroy = True
            else:
                self._idle.append(obj)
            self._cond.notify()
        if destroy:
            self._destroy(obj)  # 锁外销毁，避免阻塞其他线程

    @contextmanager
    def item(self, timeout=None):
        """上下文管理器：保证异常路径下对象一定归还（try/finally）。"""
        obj = self.acquire(timeout=timeout)
        try:
            yield obj
        finally:
            self.release(obj)

    # ---------------- 生命周期 ----------------

    def close(self):
        """关闭池：销毁全部空闲对象，唤醒所有等待者（PoolClosed）。

        在途对象在 release 时被销毁。幂等。
        """
        with self._cond:
            if self._closed:
                return
            self._closed = True
            idle, self._idle = self._idle, []
            self._cond.notify_all()
        for obj in idle:
            self._destroy(obj)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---------------- 计数与不变量 ----------------

    def _total_locked(self):
        return len(self._idle) + len(self._in_use)

    @property
    def idle_count(self):
        with self._cond:
            return len(self._idle)

    @property
    def in_use_count(self):
        with self._cond:
            return len(self._in_use)

    @property
    def total_count(self):
        with self._cond:
            return self._total_locked()

    @property
    def peak_count(self):
        with self._cond:
            return self._peak

    def check_invariants(self):
        """不变量断言：计数与实际对象数在任何时刻一致。违规抛 AssertionError。"""
        with self._cond:
            assert len(set(self._idle)) == len(self._idle), "idle 中存在重复对象"
            assert not (set(self._idle) & self._in_use), "同一对象既空闲又在用"
            assert self._total_locked() <= self._max_size, "对象总数超过上限"
            assert self._peak >= self._total_locked(), "峰值小于当前总数"
            if self._closed:
                assert not self._idle, "关闭后仍有空闲对象"
        return True
