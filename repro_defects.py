"""针对 buggy_pool.BuggyPool 稳定复现现网五类问题。

运行：python3 repro_defects.py
每复现一类问题打印一行 [REPRODUCED]，全部复现后退出码为 0。
"""

import threading
import time

from buggy_pool import BuggyPool


class FakeConnection:
    """模拟开销较大的连接对象。"""

    _seq = 0
    _seq_lock = threading.Lock()

    def __init__(self):
        with FakeConnection._seq_lock:
            FakeConnection._seq += 1
            self.ident = FakeConnection._seq
        self.closed = False

    def close(self):
        self.closed = True

    def __repr__(self):
        return f"<Conn #{self.ident} closed={self.closed}>"


def repro_1_exception_path_leak():
    """缺陷1：异常路径下对象不归还，池枯竭。"""
    pool = BuggyPool(FakeConnection, max_size=2)
    for _ in range(2):
        conn = pool.acquire()
        try:
            raise RuntimeError("业务异常")  # 忘记/无法安全归还
        except RuntimeError:
            pass  # 没有 finally: pool.release(conn)，对象泄漏
    # 池里实际已没有可用对象，但两个对象都没人持有 -> 枯竭
    before = time.monotonic()
    conn = pool.acquire(timeout=0.2)  # 只能等超时，然后走缺陷3的越界路径
    waited = time.monotonic() - before
    assert waited >= 0.19, "应当发生等待（池已枯竭）"
    stats = pool.stats()
    assert stats["idle"] == 0 and stats["total"] == 3 > 2
    print(f"[REPRODUCED] 1. 异常路径不归还 -> 池枯竭，获取阻塞 {waited:.2f}s，"
          f"且对象数 {stats['total']} 超过上限 2（泄漏对象: {conn!r}）")


def repro_2_double_release():
    """缺陷2：同一对象被归还两次后，同时被两个调用方拿到。"""
    pool = BuggyPool(FakeConnection, max_size=2)
    conn = pool.acquire()
    pool.release(conn)
    pool.release(conn)  # 重复归还，没有任何报错
    a = pool.acquire()
    b = pool.acquire()
    assert a is b, "两个调用方应拿到同一对象"
    print(f"[REPRODUCED] 2. 重复归还后同一对象被两个调用方同时持有: {a!r} == {b!r}")


def repro_3_exceeds_max_size():
    """缺陷3：获取超时后悄悄创建对象，总数超过上限。"""
    pool = BuggyPool(FakeConnection, max_size=1)
    held = pool.acquire()           # 占满唯一的坑位
    extra = pool.acquire(timeout=0.05)  # 超时后静默新建，突破上限
    assert extra is not held
    stats = pool.stats()
    assert stats["total"] == 2 > 1, "对象总数应超过 max_size"
    print(f"[REPRODUCED] 3. 超时后悄悄越界建对象: total={stats['total']} > max_size=1")


def repro_4_timeout_returns_destroyed():
    """缺陷4：超时获取返回了已被 close() 销毁的对象。"""
    pool = BuggyPool(FakeConnection, max_size=1)
    conn = pool.acquire()

    result = {}

    def waiter():
        # 池已满，等待 0.4s；期间对象被归还又被 close() 销毁
        result["conn"] = pool.acquire(timeout=0.4)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    pool.release(conn)   # 归还（不 notify，等待者仍在睡）
    time.sleep(0.1)
    pool.close()         # 销毁空闲对象，但对象仍留在空闲列表里
    t.join()
    got = result["conn"]
    assert got.closed, "超时路径返回了已销毁的对象"
    print(f"[REPRODUCED] 4. 超时获取返回已销毁对象: {got!r}")


def repro_5_counters_drift():
    """缺陷5：计数与实际对象数不一致。"""
    pool = BuggyPool(FakeConnection, max_size=2)
    a = pool.acquire()
    b = pool.acquire()
    pool.release(a)
    pool.release(a)      # 重复归还：idle 虚增、in_use 变负
    pool.release(b)
    pool.close()         # 销毁空闲对象，但 total 不扣减
    stats = pool.stats()
    actual_live = 0      # a、b 均已被 close() 销毁
    problems = []
    if stats["in_use"] != 0:
        problems.append(f"in_use={stats['in_use']}（实际 0）")
    if stats["idle"] != actual_live:
        problems.append(f"idle={stats['idle']}（实际存活 {actual_live}）")
    if stats["total"] != actual_live:
        problems.append(f"total={stats['total']}（实际存活 {actual_live}）")
    assert problems, "计数应当漂移"
    print(f"[REPRODUCED] 5. 计数与实际不一致: {', '.join(problems)}")


if __name__ == "__main__":
    repro_1_exception_path_leak()
    repro_2_double_release()
    repro_3_exceeds_max_size()
    repro_4_timeout_returns_destroyed()
    repro_5_counters_drift()
    print("全部 5 类问题均已复现。修复后的实现见 object_pool.py，"
          "回归测试见 test_object_pool.py。")
