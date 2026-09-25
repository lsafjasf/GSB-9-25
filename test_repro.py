"""复现测试：针对 pool_buggy.BuggyPool，稳定复现现网五类问题。

每个测试断言的是"缺陷症状确实发生"，因此本文件对 buggy 实现应全部通过，
对修复后的 ObjectPool 则对应场景由 test_pool_fixed.py 断言正确行为。
"""

import threading
import time
import unittest

from pool_buggy import BuggyPool


class FakeConnection:
    """模拟开销较大的连接对象。"""

    _seq = 0
    _seq_lock = threading.Lock()

    def __init__(self):
        with FakeConnection._seq_lock:
            FakeConnection._seq += 1
            self.id = FakeConnection._seq
        self.closed = False

    def close(self):
        self.closed = True


def make_pool(max_size=2):
    return BuggyPool(FakeConnection, max_size)


class TestReproDefects(unittest.TestCase):
    def test_defect1_exception_path_leaks_object(self):
        """缺陷1：异常路径下对象不归还，池枯竭。"""
        pool = make_pool(max_size=1)
        obj = pool.get_or_create()
        try:
            raise RuntimeError("business error")
        except RuntimeError:
            pass  # 缺陷：没有 finally: pool.release(obj)，也没有上下文管理器可用
        # 池已枯竭：唯一的对象泄漏，再次获取只能超时返回 None
        self.assertIsNone(pool.acquire(timeout=0.1))

    def test_defect2_double_release_same_object_twice_acquired(self):
        """缺陷2：同一对象被归还两次后，同时被两个调用方拿到。"""
        pool = make_pool(max_size=1)
        obj = pool.get_or_create()
        pool.release(obj)
        pool.release(obj)  # 重复归还未被拒绝
        a = pool.acquire(timeout=0.1)
        b = pool.acquire(timeout=0.1)
        self.assertIs(a, obj)
        self.assertIs(b, obj)  # 同一对象同时被两个调用方持有
        self.assertIs(a, b)

    def test_defect3_silently_exceeds_max_size(self):
        """缺陷3：超过上限时悄悄创建对象。"""
        pool = make_pool(max_size=2)
        objs = [pool.get_or_create() for _ in range(5)]  # 全部不归还
        self.assertEqual(len({o.id for o in objs}), 5)   # 真的创建了 5 个
        self.assertGreater(pool.total, 2)                # 超过 max_size=2

    def test_defect4_timeout_returns_destroyed_object(self):
        """缺陷4：超时获取返回了已被销毁的对象。"""
        pool = make_pool(max_size=1)
        obj = pool.get_or_create()
        pool.release(obj)
        pool.close()                 # 空闲对象被销毁，但进了 _destroyed
        self.assertTrue(obj.closed)
        got = pool.acquire(timeout=0.05)  # 超时路径捡回已销毁对象
        self.assertIs(got, obj)
        self.assertTrue(got.closed)

    def test_defect5_counters_inconsistent(self):
        """缺陷5：计数与实际对象数不一致。"""
        pool = make_pool(max_size=2)
        a = pool.get_or_create()
        b = pool.get_or_create()
        pool.release(a)
        pool.release(b)
        # 实际：在用 0、空闲 2；但 in_use 计数仍是 2
        self.assertEqual(pool.in_use, 2)
        pool.close()
        # close 后实际存活对象为 0，但 total 仍是 2
        self.assertEqual(pool.total, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
