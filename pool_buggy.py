"""有缺陷的对象池实现（仅用于复现现网五类问题，请勿在生产使用）。

缺陷清单：
1. 无上下文管理器 / finally 结构，异常路径下对象不归还 -> 池枯竭。
2. release() 不校验对象归属与重复归还 -> 同一对象被归还两次后，
   会同时被两个调用方拿到。
3. acquire() 在空闲为空时不检查上限，悄悄创建超过 max_size 的对象。
4. acquire(timeout=...) 超时后返回已被销毁的对象（从 _destroyed 里捡）。
5. 计数（in_use / total / peak）与实际对象数不一致：
   release 不递减 in_use，close 不清空 idle 也不修正 total。
"""

import threading
import time


class BuggyPool:
    def __init__(self, factory, max_size):
        self._factory = factory
        self._max_size = max_size
        self._idle = []
        self._destroyed = []          # 缺陷4：销毁的对象仍被保留并可被返回
        self._cond = threading.Condition()
        self._closed = False
        # 对外暴露的计数（缺陷5：维护不正确）
        self.in_use = 0
        self.total = 0
        self.peak = 0

    def acquire(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self._idle:
                if timeout is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # 缺陷4：超时返回已销毁对象，而不是报错
                        if self._destroyed:
                            self.in_use += 1
                            return self._destroyed.pop()
                        return None
                    self._cond.wait(remaining)
                else:
                    self._cond.wait()
            obj = self._idle.pop()
            self.in_use += 1
            return obj

    def get_or_create(self):
        """缺陷3：空闲为空时不检查 max_size，直接创建，导致超限。"""
        with self._cond:
            if self._idle:
                self.in_use += 1
                return self._idle.pop()
            self.total += 1
            self.in_use += 1
            self.peak = max(self.peak, self.total)
            return self._factory()

    def release(self, obj):
        with self._cond:
            # 缺陷2：不检查 obj 是否真的处于"在用"状态，重复归还直接入列
            self._idle.append(obj)
            # 缺陷5：in_use 从不递减
            self._cond.notify()

    def close(self):
        with self._cond:
            self._closed = True
            for obj in self._idle:
                close = getattr(obj, "close", None)
                if close:
                    close()
                self._destroyed.append(obj)   # 缺陷4 的源头
            # 缺陷5：idle 未清空、total 未修正、等待者未被唤醒
