"""eventbus 自测：python3 -m unittest test_eventbus -v"""

import gc
import threading
import unittest

from eventbus import EventBus


class TestBasicSemantics(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_subscribe_publish_and_order(self):
        calls = []
        self.bus.subscribe("evt", lambda v: calls.append(("a", v)))
        self.bus.subscribe("evt", lambda v: calls.append(("b", v)))
        errors = self.bus.publish("evt", 1)
        self.assertEqual(errors, [])
        self.assertEqual(calls, [("a", 1), ("b", 1)])  # 订阅顺序即投递顺序

    def test_event_type_isolation(self):
        calls = []
        self.bus.subscribe("x", lambda: calls.append("x"))
        self.bus.subscribe("y", lambda: calls.append("y"))
        self.bus.publish("x")
        self.assertEqual(calls, ["x"])

    def test_once(self):
        calls = []
        self.bus.subscribe("evt", lambda: calls.append(1), once=True)
        self.bus.publish("evt")
        self.bus.publish("evt")
        self.assertEqual(calls, [1])
        self.assertEqual(self.bus.subscriber_count("evt"), 0)

    def test_filter(self):
        calls = []
        self.bus.subscribe("evt", lambda v: calls.append(v),
                           filter=lambda v: v % 2 == 0)
        self.bus.publish("evt", 1)
        self.bus.publish("evt", 2)
        self.assertEqual(calls, [2])

    def test_unsubscribe_by_callback(self):
        calls = []

        def handler():
            calls.append(1)

        self.bus.subscribe("evt", handler)
        self.bus.unsubscribe("evt", handler)
        self.bus.publish("evt")
        self.assertEqual(calls, [])

    def test_cancel_idempotent_and_unknown(self):
        sub = self.bus.subscribe("evt", lambda: None)
        sub.cancel()
        sub.cancel()  # 重复取消不报错
        self.bus.unsubscribe("evt", lambda: None)  # 取消不存在的订阅不报错
        self.bus.unsubscribe("nope", lambda: None)
        self.assertEqual(self.bus.subscriber_count(), 0)

    def test_empty_event_type(self):
        # 空字符串是合法事件类型
        calls = []
        self.bus.subscribe("", lambda: calls.append(1))
        self.assertEqual(self.bus.publish(""), [])
        self.assertEqual(calls, [1])
        # 对无订阅者的类型发布是空操作
        self.assertEqual(self.bus.publish("nothing-here"), [])


class TestUnsubscribeImmediacy(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_cancel_takes_effect_in_current_round(self):
        # 前面的订阅者取消后面的订阅者：后者本轮不得被调用
        calls = []
        sub_b = None

        def a():
            calls.append("a")
            sub_b.cancel()

        def b():
            calls.append("b")

        self.bus.subscribe("evt", a)
        sub_b = self.bus.subscribe("evt", b)
        self.bus.publish("evt")
        self.assertEqual(calls, ["a"])

    def test_self_unsubscribe(self):
        # 订阅者自取消：本轮已执行，后续轮次不再调用；行为确定
        calls = []
        holder = {}

        def me():
            calls.append(1)
            holder["sub"].cancel()

        holder["sub"] = self.bus.subscribe("evt", me)
        self.bus.publish("evt")
        self.bus.publish("evt")
        self.assertEqual(calls, [1])
        self.assertEqual(self.bus.subscriber_count("evt"), 0)

    def test_unsubscribe_during_other_thread_publish(self):
        # 取消与并发投递竞争：取消返回后不得再有任何一次调用
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow():
            started.set()
            release.wait(5)
            calls.append("slow")

        def target():
            calls.append("target")

        self.bus.subscribe("evt", slow)
        sub = self.bus.subscribe("evt", target)
        t = threading.Thread(target=self.bus.publish, args=("evt",))
        t.start()
        started.wait(5)
        sub.cancel()
        release.set()
        t.join(5)
        # 取消返回后，本轮及后续任何轮次都不得再调用 target
        self.assertNotIn("target", calls)
        self.bus.publish("evt")
        self.assertNotIn("target", calls)


class TestExceptionAggregation(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_exceptions_collected_and_others_still_called(self):
        calls = []

        def bad1():
            raise ValueError("boom1")

        def bad2():
            raise KeyError("boom2")

        self.bus.subscribe("evt", bad1)
        self.bus.subscribe("evt", lambda: calls.append("ok"))
        self.bus.subscribe("evt", bad2)
        errors = self.bus.publish("evt")
        self.assertEqual(calls, ["ok"])  # 异常不中断其他订阅者
        self.assertEqual(len(errors), 2)
        self.assertIsInstance(errors[0], ValueError)
        self.assertIsInstance(errors[1], KeyError)

    def test_filter_exception_aggregated(self):
        calls = []
        self.bus.subscribe("evt", lambda: calls.append(1),
                           filter=lambda: 1 / 0)
        errors = self.bus.publish("evt")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ZeroDivisionError)
        self.assertEqual(calls, [])

    def test_once_removed_even_if_callback_raises(self):
        self.bus.subscribe("evt", lambda: 1 / 0, once=True)
        self.assertEqual(len(self.bus.publish("evt")), 1)
        self.assertEqual(self.bus.subscriber_count("evt"), 0)


class TestMutationDuringPublish(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_add_during_publish_not_called_this_round(self):
        calls = []

        def a():
            calls.append("a")
            self.bus.subscribe("evt", lambda: calls.append("late"))

        self.bus.subscribe("evt", a)
        self.bus.publish("evt")
        self.assertEqual(calls, ["a"])  # 新增者本轮不投递
        self.bus.publish("evt")
        self.assertEqual(calls, ["a", "a", "late"])  # 下一轮正常投递，无重复无遗漏

    def test_remove_during_publish_no_duplicate_no_missing(self):
        calls = []
        sub_c = {}

        def a():
            calls.append("a")
            sub_c["sub"].cancel()

        self.bus.subscribe("evt", a)
        self.bus.subscribe("evt", lambda: calls.append("b"))
        sub_c["sub"] = self.bus.subscribe("evt", lambda: calls.append("c"))
        self.bus.publish("evt")
        self.assertEqual(calls, ["a", "b"])  # c 被取消，本轮跳过；b 不受影响

    def test_recursive_publish_same_event(self):
        calls = []
        depth = {"n": 0}

        def handler():
            depth["n"] += 1
            calls.append(depth["n"])
            if depth["n"] < 3:
                self.bus.publish("evt")  # 递归触发同类事件

        self.bus.subscribe("evt", handler)
        self.bus.publish("evt")
        self.assertEqual(calls, [1, 2, 3])  # 每次发布独立快照，行为确定
        self.assertEqual(self.bus.subscriber_count("evt"), 1)


class TestWeakReference(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_weak_subscriber_collectable(self):
        calls = []

        class Listener:
            def on_event(self):
                calls.append(1)

        listener = Listener()
        self.bus.subscribe("evt", listener.on_event, weak=True)
        self.assertEqual(self.bus.subscriber_count("evt"), 1)

        ref_alive = lambda: self.bus.subscriber_count("evt")
        del listener
        gc.collect()
        # 对象已回收，订阅自动失效并清除
        self.assertEqual(ref_alive(), 0)
        self.bus.publish("evt")
        self.assertEqual(calls, [])

    def test_weak_subscriber_alive_until_released(self):
        calls = []

        class Listener:
            def on_event(self, v):
                calls.append(v)

        listener = Listener()
        self.bus.subscribe("evt", listener.on_event, weak=True)
        self.bus.publish("evt", 42)
        self.assertEqual(calls, [42])
        del listener
        gc.collect()
        self.bus.publish("evt", 43)
        self.assertEqual(calls, [42])

    def test_strong_subscription_keeps_object_alive(self):
        calls = []

        class Listener:
            def on_event(self):
                calls.append(1)

        listener = Listener()
        self.bus.subscribe("evt", listener.on_event)  # 默认强引用
        del listener
        gc.collect()
        self.bus.publish("evt")
        self.assertEqual(calls, [1])  # 强引用订阅阻止回收（文档化行为）


class TestConcurrency(unittest.TestCase):
    def test_concurrent_publish_and_subscribe(self):
        bus = EventBus()
        counter = []
        lock = threading.Lock()
        stop = threading.Event()

        def stable(v):
            with lock:
                counter.append(v)

        bus.subscribe("evt", stable)
        n_publishers = 8
        per_publisher = 500

        def publisher():
            for i in range(per_publisher):
                bus.publish("evt", i)

        def churner():
            # 并发订阅/取消，不得破坏订阅列表结构
            while not stop.is_set():
                sub = bus.subscribe("evt", lambda v: None)
                sub.cancel()

        threads = [threading.Thread(target=publisher) for _ in range(n_publishers)]
        churners = [threading.Thread(target=churner) for _ in range(4)]
        for t in churners + threads:
            t.start()
        for t in threads:
            t.join(30)
        stop.set()
        for t in churners:
            t.join(30)

        # stable 每次发布都被调用一次：总数精确等于发布次数
        self.assertEqual(len(counter), n_publishers * per_publisher)
        # 注册表结构完好：churner 的订阅全部成对注销，只剩 stable
        self.assertEqual(bus.subscriber_count("evt"), 1)
        self.assertEqual(bus.subscriber_count(), 1)

    def test_once_fires_exactly_once_under_concurrency(self):
        bus = EventBus()
        calls = []
        lock = threading.Lock()

        def handler():
            with lock:
                calls.append(1)

        bus.subscribe("evt", handler, once=True)
        threads = [threading.Thread(target=bus.publish, args=("evt",))
                   for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(calls), 1)
        self.assertEqual(bus.subscriber_count("evt"), 0)


if __name__ == "__main__":
    unittest.main()
