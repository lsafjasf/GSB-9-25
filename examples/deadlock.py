"""死锁：两个线程以相反顺序获取两把锁。

框架版：调度器在「无可运行线程且仍有阻塞线程」时抛出 DeadlockError，
        确定性复现，不会真的挂死。
真实版：threading.Lock 一旦死锁就是永久挂起，压测里只能用超时间接观察，
        且窗口极小、很难撞上。
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from racefw import Lock, explore, format_trace  # noqa: E402


# --------------------------------------------------------------------- #
# 框架场景
# --------------------------------------------------------------------- #
def scenario(sched):
    lock_a = Lock(sched, "A")
    lock_b = Lock(sched, "B")

    def t1():
        yield from lock_a.acquire()
        yield from sched.preempt("holding A, about to request B")
        yield from lock_b.acquire()
        yield from lock_b.release()
        yield from lock_a.release()

    def t2():
        yield from lock_b.acquire()
        yield from sched.preempt("holding B, about to request A")
        yield from lock_a.acquire()
        yield from lock_a.release()
        yield from lock_b.release()

    sched.spawn(t1, name="t1(A->B)")
    sched.spawn(t2, name="t2(B->A)")


def outcome_fn(sched):
    return "completed"


# --------------------------------------------------------------------- #
# 真实线程对照：用 join 超时探测死锁（daemon 线程避免拖死进程）
# --------------------------------------------------------------------- #
def real_once(timeout=0.3):
    a = threading.Lock()
    b = threading.Lock()

    def t1():
        with a:
            with b:
                pass

    def t2():
        with b:
            with a:
                pass

    ts = [threading.Thread(target=t1, daemon=True),
          threading.Thread(target=t2, daemon=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout)
    return any(t.is_alive() for t in ts)   # True => 发生死锁


def stress(runs=100):
    bad = sum(1 for _ in range(runs) if real_once())
    return bad, runs


if __name__ == "__main__":
    res = explore(scenario, runs=30, strategy="pct", outcome_fn=outcome_fn)
    print(res.report("deadlock / framework(pct)"))
    if res.failures:
        seed, kind, s = res.failures[0]
        print(f"\n[bug reproduced] seed={seed} kind={kind}")
        print("[event trace]\n" + format_trace(s))
    bad, total = stress()
    print(f"\n[real-thread stress] deadlocks={bad}/{total} "
          f"(rate={bad / total:.2%})")
