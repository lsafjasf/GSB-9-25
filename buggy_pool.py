"""修复前的对象池实现（保留缺陷，用于复现现网五类问题）。

缺陷清单（与 repro_defects.py 一一对应）：
  1. 没有安全的获取/归还 API，异常路径下对象不归还，池最终枯竭；
  2. release() 不做任何校验，同一对象可被归还两次，之后被两个调用方同时拿到；
  3. 获取超时后“悄悄”新建对象，实际对象数超过 max_size 上限；
  4. close() 销毁空闲对象后仍把它们留在空闲列表里，超时路径会把
     已销毁的对象返回给调用方；
  5. 计数（_created/_in_use）与真实对象数不同步：销毁不扣减、
     重复归还重复计数，stats() 与实际状态漂移。
"""

import threading


class BuggyPool:
    def __init__(self, factory, max_size=4):
        self._factory = factory
        self._max_size = max_size
        self._idle = []
        self._created = 0   # 缺陷5：只增不减，销毁对象后不同步
        self._in_use = 0    # 缺陷5：重复归还会被重复扣减成负数
        self._peak = 0
        self._closed = False
        self._cond = threading.Condition()

    def acquire(self, timeout=None):
        with self._cond:
            if self._closed:
                raise RuntimeError("pool is closed")
            if self._idle:
                self._in_use += 1
                return self._idle.pop()
            if self._created < self._max_size:
                self._created += 1
                self._in_use += 1
                self._peak = max(self._peak, self._created)
                return self._factory()
            # 池已满：等待。缺陷：release() 不 notify，等待者只能睡到超时。
            self._cond.wait(timeout)
            if self._idle:
                # 缺陷4：不做任何有效性检查，可能弹出已被 close() 销毁的对象
                self._in_use += 1
                return self._idle.pop()
            # 缺陷3：超时后悄悄突破上限新建对象
            self._created += 1
            self._in_use += 1
            return self._factory()

    def release(self, obj):
        with self._cond:
            # 缺陷2：不校验对象是否属于本池/是否已归还，直接入列
            self._idle.append(obj)
            self._in_use -= 1
            # 缺陷：忘记 notify，等待中的 acquire 只能等到超时

    def close(self):
        with self._cond:
            self._closed = True
            for obj in self._idle:
                obj.close()  # 缺陷4：销毁了却不从 _idle 移除
            # 缺陷5：_created 不随销毁扣减；也不唤醒等待者

    def stats(self):
        with self._cond:
            return {
                "idle": len(self._idle),
                "in_use": self._in_use,
                "total": self._created,
                "peak": self._peak,
            }
