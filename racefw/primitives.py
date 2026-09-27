"""racefw.primitives — 可注入调度器的同步原语。

均为生成器式 API，在线程代码中以 `yield from` 使用；
原语内部在关键位置自动产生切换点，无需用户手动插入。
"""
from collections import deque

from .core import _Block


class Lock:
    """互斥锁（FIFO 公平，不可重入）。"""

    def __init__(self, sched, name):
        self.sched = sched
        self.name = name
        self.owner = None
        self.waiters = deque()

    def acquire(self):
        s = self.sched
        tid = s.current
        s.log("lock.try", resource=self.name, depth=2)
        if self.owner is None and not self.waiters:
            self.owner = tid
            s.log("lock.acquired", resource=self.name, depth=2)
        else:
            self.waiters.append(tid)
            yield _Block(self.name)
            # 被唤醒时锁所有权已移交
            s.log("lock.acquired", resource=self.name, detail="(after wakeup)", depth=2)
        yield from s.preempt(f"after acquiring {self.name}")

    def release(self):
        s = self.sched
        tid = s.current
        assert self.owner == tid, f"{self.name} released by non-owner {tid}"
        s.log("lock.release", resource=self.name, depth=2)
        self._release_now()
        yield from s.preempt(f"after releasing {self.name}")

    def _release_now(self):
        """释放锁并移交等待者，不产生切换点（供 Condition 原子使用）。"""
        s = self.sched
        if self.waiters:
            nxt = self.waiters.popleft()
            self.owner = nxt
            s.wake(nxt, f"lock {self.name} handed off")
        else:
            self.owner = None


class Semaphore:
    """计数信号量（FIFO 公平）。"""

    def __init__(self, sched, name, value=1):
        self.sched = sched
        self.name = name
        self.value = value
        self.waiters = deque()

    def acquire(self):
        s = self.sched
        tid = s.current
        s.log("sem.try", resource=self.name, detail=f"value={self.value}", depth=2)
        if self.value > 0 and not self.waiters:
            self.value -= 1
            s.log("sem.acquired", resource=self.name, detail=f"value={self.value}", depth=2)
        else:
            self.waiters.append(tid)
            yield _Block(self.name)
            s.log("sem.acquired", resource=self.name, detail="(after wakeup)", depth=2)
        yield from s.preempt(f"after acquiring {self.name}")

    def release(self):
        s = self.sched
        s.log("sem.release", resource=self.name, depth=2)
        if self.waiters:
            nxt = self.waiters.popleft()
            s.wake(nxt, f"semaphore {self.name} handed off")
        else:
            self.value += 1
        yield from s.preempt(f"after releasing {self.name}")


class Condition:
    """条件变量，绑定一把 Lock。语义同 threading.Condition 的 wait/notify。"""

    def __init__(self, sched, lock, name):
        self.sched = sched
        self.lock = lock
        self.name = name
        self.waiters = deque()

    def wait(self):
        s = self.sched
        tid = s.current
        assert self.lock.owner == tid, "Condition.wait requires holding the lock"
        s.log("cond.wait", resource=self.name, depth=2)
        # 与 threading.Condition 一致：释放锁与阻塞是原子的，中间不允许切换，
        # 否则 notify 可能发生在两者之间而丢失唤醒。
        self.lock._release_now()
        self.waiters.append(tid)
        yield _Block(self.name)
        s.log("cond.woken", resource=self.name, depth=2)
        yield from self.lock.acquire()

    def notify(self):
        s = self.sched
        assert self.lock.owner == s.current, "Condition.notify requires holding the lock"
        s.log("cond.notify", resource=self.name, depth=2)
        if self.waiters:
            nxt = self.waiters.popleft()
            s.wake(nxt, f"condition {self.name} signaled")
        yield from s.preempt(f"after notify {self.name}")
