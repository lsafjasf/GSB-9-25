import asyncio
import unittest

from rrc import (
    BusyError,
    ChannelError,
    Client,
    RequestTimeout,
    SimulatedChannel,
)


class CorrelationTest(unittest.IsolatedAsyncioTestCase):
    async def test_reorder_all_matched(self):
        """乱序（随机延迟）下 200 个并发请求全部正确配对。"""
        ch = SimulatedChannel(latency=(0.0, 0.05), seed=1)
        c = Client(ch, max_inflight=200, on_full="queue")
        results = await asyncio.gather(*(c.call(f"req-{i}", timeout=2.0) for i in range(200)))
        self.assertEqual(results, [f"req-{i}" for i in range(200)])
        self.assertEqual(c.stats["succeeded"], 200)
        self.assertEqual(c.stats["unknown"], 0)
        c.check_consistent()

    async def test_timeout_isolation(self):
        """超时隔离：调用方立即拿到超时；迟到响应被丢弃且不改写任何状态。"""
        ch = SimulatedChannel(latency=(0.2, 0.2), seed=2)
        c = Client(ch)
        with self.assertRaises(RequestTimeout):
            await c.call("slow", timeout=0.05)
        # 调用方已拿到结果，关联表立即清空
        self.assertEqual(c.inflight, 0)
        self.assertEqual(c.stats["timed_out"], 1)
        self.assertEqual(c.stats["succeeded"], 0)
        # 等待迟到响应到达
        await asyncio.sleep(0.3)
        self.assertEqual(c.stats["late"], 1)          # 被识别为迟到并丢弃
        self.assertEqual(c.stats["succeeded"], 0)     # 没有改写任何状态
        self.assertEqual(c.stats["timed_out"], 1)
        # 后续请求不受污染，结果正确
        self.assertEqual(await c.call("fast", timeout=1.0), "fast")
        self.assertEqual(c.stats["succeeded"], 1)
        c.check_consistent()

    async def test_unknown_and_duplicate(self):
        """未知 id 与重复响应（重放）分别计数。"""
        ch = SimulatedChannel(latency=(0.001, 0.001), replay=1.0, seed=3)
        c = Client(ch)
        self.assertEqual(await c.call("x", timeout=1.0), "x")
        await asyncio.sleep(0.05)  # 等重放的副本到达
        self.assertEqual(c.stats["succeeded"], 1)
        self.assertEqual(c.stats["duplicate"], 1)
        ch.inject({"id": 999999, "payload": "ghost"})
        self.assertEqual(c.stats["unknown"], 1)
        c.check_consistent()

    async def test_concurrency_limit_fail_fast(self):
        """on_full='fail'：达到上限立即快速失败。"""
        ch = SimulatedChannel(latency=(0.1, 0.1), seed=4)
        c = Client(ch, max_inflight=2, on_full="fail")
        t1 = asyncio.create_task(c.call("a", timeout=1.0))
        t2 = asyncio.create_task(c.call("b", timeout=1.0))
        await asyncio.sleep(0.01)
        with self.assertRaises(BusyError):
            await c.call("c", timeout=1.0)
        self.assertEqual(await t1, "a")
        self.assertEqual(await t2, "b")
        self.assertEqual(c.stats["rejected"], 1)
        c.check_consistent()

    async def test_concurrency_limit_queue(self):
        """on_full='queue'：排队执行，峰值并发不超过上限。"""
        ch = SimulatedChannel(latency=(0.01, 0.03), seed=5)
        c = Client(ch, max_inflight=3, on_full="queue")
        results = await asyncio.gather(*(c.call(i, timeout=2.0) for i in range(20)))
        self.assertEqual(results, list(range(20)))
        self.assertLessEqual(c.stats["peak_inflight"], 3)
        c.check_consistent()

    async def test_explicit_cancel(self):
        """显式取消：关联表与计时器立即回收，迟到响应计 late。"""
        ch = SimulatedChannel(latency=(0.2, 0.2), seed=6)
        c = Client(ch)
        task = asyncio.create_task(c.call("x", timeout=5.0))
        await asyncio.sleep(0.02)
        rid = next(iter(c._pending))
        self.assertTrue(c.cancel(rid))
        self.assertEqual(c.inflight, 0)               # 立即回收
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(c.stats["cancelled"], 1)
        await asyncio.sleep(0.3)                      # 迟到响应到达
        self.assertEqual(c.stats["late"], 1)
        self.assertEqual(c.stats["succeeded"], 0)
        c.check_consistent()

    async def test_task_cancel_cleans_up(self):
        """取消调用方协程同样立即清理悬挂状态。"""
        ch = SimulatedChannel(latency=(0.2, 0.2), seed=7)
        c = Client(ch)
        task = asyncio.create_task(c.call("x", timeout=5.0))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(c.inflight, 0)
        self.assertEqual(c.stats["cancelled"], 1)
        c.check_consistent()

    async def test_disconnect_fail_policy(self):
        """断开时 fail 策略：在途请求立即失败，绝不悬挂。"""
        ch = SimulatedChannel(latency=(0.2, 0.2), seed=8)
        c = Client(ch, on_disconnect="fail")
        tasks = [asyncio.create_task(c.call(i, timeout=5.0)) for i in range(3)]
        await asyncio.sleep(0.02)
        ch.disconnect()
        for t in tasks:
            with self.assertRaises(ChannelError):
                await t
        self.assertEqual(c.inflight, 0)
        self.assertEqual(c.stats["failed"], 3)
        ch.reconnect()
        self.assertEqual(await c.call("after", timeout=1.0), "after")
        c.check_consistent()

    async def test_disconnect_retry_policy(self):
        """断开时 retry 策略：重连后重发并得到确定结果。"""
        ch = SimulatedChannel(latency=(0.02, 0.02), seed=9)
        c = Client(ch, on_disconnect="retry")
        ch.disconnect()
        task = asyncio.create_task(c.call("buffered", timeout=2.0))
        await asyncio.sleep(0.05)
        self.assertEqual(c.stats["buffered"], 1)      # 发送被缓冲而非丢失
        ch.reconnect()
        self.assertEqual(await task, "buffered")      # 重发后成功
        self.assertEqual(c.stats["resent"], 1)
        c.check_consistent()

    async def test_retry_still_bounded_by_timeout(self):
        """retry 策略下若一直不重连，请求按超时确定失败，不永远等待。"""
        ch = SimulatedChannel(latency=(0.01, 0.01), seed=10)
        c = Client(ch, on_disconnect="retry")
        ch.disconnect()
        with self.assertRaises(RequestTimeout):
            await c.call("doomed", timeout=0.1)
        self.assertEqual(c.inflight, 0)
        c.check_consistent()



class TombstoneTest(unittest.IsolatedAsyncioTestCase):
    """墓碑表：容量/TTL 可配置，四类响应分类计数。"""

    async def test_capacity_eviction_counts_expired(self):
        """容量驱逐：墓碑被挤出后，迟到的响应计 expired 而非 duplicate。"""
        ch = SimulatedChannel(latency=(0.001, 0.001), seed=11)
        c = Client(ch, tombstone_size=1)
        self.assertEqual(await c.call("a", timeout=1.0), "a")   # rid=1
        self.assertEqual(await c.call("b", timeout=1.0), "b")   # rid=2, 挤出 rid=1
        ch.inject({"id": 1, "payload": "a"})   # 墓碑已被容量驱逐
        ch.inject({"id": 2, "payload": "b"})   # 墓碑仍在
        self.assertEqual(c.stats["expired"], 1)
        self.assertEqual(c.stats["duplicate"], 1)
        self.assertEqual(c.stats["unknown"], 0)
        c.check_consistent()

    async def test_ttl_expiry_counts_expired(self):
        """TTL 过期：存活期内计 duplicate，过期后计 expired。"""
        ch = SimulatedChannel(latency=(0.001, 0.001), seed=12)
        c = Client(ch, tombstone_size=100, tombstone_ttl=0.05)
        self.assertEqual(await c.call("x", timeout=1.0), "x")   # rid=1
        ch.inject({"id": 1, "payload": "x"})
        self.assertEqual(c.stats["duplicate"], 1)
        await asyncio.sleep(0.08)                # 超过 TTL
        ch.inject({"id": 1, "payload": "x"})
        self.assertEqual(c.stats["expired"], 1)
        self.assertEqual(c.stats["duplicate"], 1)
        c.check_consistent()

    async def test_unknown_vs_expired_by_id_range(self):
        """id 大于已发序号 => unknown；id 在已发范围内但墓碑不在 => expired。"""
        ch = SimulatedChannel(latency=(0.001, 0.001), seed=13)
        c = Client(ch, tombstone_size=0)         # 不保留任何墓碑
        self.assertEqual(await c.call("x", timeout=1.0), "x")   # rid=1
        ch.inject({"id": 1, "payload": "x"})     # 已发范围 => expired
        ch.inject({"id": 999, "payload": "g"})   # 未发序号 => unknown
        ch.inject({"id": -3, "payload": "g"})    # 非法 id => unknown
        self.assertEqual(c.stats["expired"], 1)
        self.assertEqual(c.stats["unknown"], 2)
        self.assertEqual(c.stats["duplicate"], 0)
        self.assertEqual(c.stats["late"], 0)
        c.check_consistent()

    async def test_late_sub_reasons(self):
        """迟到响应按子原因分别计数：late_timeout / late_cancelled。"""
        ch = SimulatedChannel(latency=(0.2, 0.2), seed=14)
        c = Client(ch)
        with self.assertRaises(RequestTimeout):
            await c.call("slow", timeout=0.05)   # rid=1, 超时
        task = asyncio.create_task(c.call("x", timeout=5.0))  # rid=2
        await asyncio.sleep(0.02)
        c.cancel(2)
        await asyncio.sleep(0.3)                 # 两个迟到响应到达
        self.assertEqual(c.stats["late"], 2)
        self.assertEqual(c.stats["late_timeout"], 1)
        self.assertEqual(c.stats["late_cancelled"], 1)
        self.assertEqual(c.stats["late_failed"], 0)
        c.check_consistent()

    async def test_tombstone_memory_bounded(self):
        """内存上界可复算：实测占用 <= 推导上界，与请求总量无关。"""
        ch = SimulatedChannel(latency=(0.0, 0.001), seed=15)
        c = Client(ch, tombstone_size=64)
        for i in range(500):
            await c.call(i, timeout=1.0)
        self.assertLessEqual(len(c._tombstones), 64)
        self.assertLessEqual(c.tombstone_memory(), c.tombstone_bound())
        c.check_consistent()


if __name__ == "__main__":
    unittest.main()
