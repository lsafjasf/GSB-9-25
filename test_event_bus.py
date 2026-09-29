"""event_bus 的自测（仅标准库 unittest）。

运行：python3 -m unittest test_event_bus -v
"""

import gc
import threading
import unittest
import weakref

from event_bus import DeliveryError, EventBus, topic_matches


class TestBasicSemantics(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_subscribe_and_publish_in_subscription_order(self):
        calls = []
        self.bus.subscribe("e", lambda v: calls.append(("a", v)))
        self.bus.subscribe("e", lambda v: calls.append(("b", v)))
        self.bus.subscribe("e", lambda v: calls.append(("c", v)))
        errors = self.bus.publish("e", 42)
        self.assertEqual(errors, [])
        self.assertEqual(calls, [("a", 42), ("b", 42), ("c", 42)])

    def test_event_type_isolation_and_kwargs(self):
        calls = []
        self.bus.subscribe("x", lambda **kw: calls.append(kw))
        self.bus.publish("y", payload=1)
        self.assertEqual(calls, [])
        self.bus.publish("x", payload=2)
        self.assertEqual(calls, [{"payload": 2}])

    def test_once(self):
        calls = []
        self.bus.subscribe_once("e", lambda v: calls.append(v))
        self.bus.publish("e", 1)
        self.bus.publish("e", 2)
        self.assertEqual(calls, [1])
        self.assertEqual(self.bus.subscriber_count("e"), 0)

    def test_once_with_filter_not_consumed_when_filtered_out(self):
        calls = []
        self.bus.subscribe_once("e", lambda v: calls.append(v),
                                filter=lambda v: v > 0)
        self.bus.publish("e", -1)  # 过滤不通过，不消耗 once
        self.bus.publish("e", 5)
        self.bus.publish("e", 6)
        self.assertEqual(calls, [5])

    def test_filter(self):
        calls = []
        self.bus.subscribe("e", lambda v: calls.append(v),
                           filter=lambda v: v % 2 == 0)
        self.bus.publish("e", 1)
        self.bus.publish("e", 2)
        self.bus.publish("e", 3)
        self.bus.publish("e", 4)
        self.assertEqual(calls, [2, 4])

    def test_unsubscribe(self):
        calls = []
        sub = self.bus.subscribe("e", lambda: calls.append(1))
        self.bus.publish("e")
        sub.cancel()
        self.bus.publish("e")
        self.assertEqual(calls, [1])
        self.assertFalse(sub.active)
        self.assertEqual(self.bus.subscriber_count("e"), 0)

    def test_double_unsubscribe_is_silent(self):
        sub = self.bus.subscribe("e", lambda: None)
        self.assertTrue(self.bus.unsubscribe(sub))
        self.assertFalse(self.bus.unsubscribe(sub))  # 重复取消不报错
        sub.cancel()  # 通过句柄重复取消也不报错
        self.assertFalse(sub.active)

    def test_empty_event_type(self):
        # 空字符串是合法事件类型
        calls = []
        self.bus.subscribe("", lambda v: calls.append(v))
        errors = self.bus.publish("", "ping")
        self.assertEqual(errors, [])
        self.assertEqual(calls, ["ping"])
        # 无任何订阅者的类型：正常返回空错误列表
        self.assertEqual(self.bus.publish("nobody-listens", 1), [])

    def test_clear(self):
        self.bus.subscribe("a", lambda: None)
        self.bus.subscribe("b", lambda: None)
        self.bus.clear()
        self.assertEqual(self.bus.subscriber_count(), 0)


class TestCancelDuringDispatch(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_cancel_later_subscriber_takes_effect_immediately(self):
        # 确定规则：A 取消尚未执行的 B 后，B 在本轮剩余投递中不再被调用，
        # 其余订阅者（C）不受影响
        calls = []
        state = {}
        def a():
            calls.append("a")
            state["b"].cancel()
        self.bus.subscribe("e", a)
        state["b"] = self.bus.subscribe("e", lambda: calls.append("b"))
        self.bus.subscribe("e", lambda: calls.append("c"))
        self.bus.publish("e")
        self.assertEqual(calls, ["a", "c"])  # b 被取消，本轮不再调用

    def test_self_unsubscribe_during_dispatch(self):
        # 确定规则：自取消只影响自己，后续订阅者照常投递
        calls = []
        state = {}
        def a():
            calls.append("a")
            state["a"].cancel()
        state["a"] = self.bus.subscribe("e", a)
        self.bus.subscribe("e", lambda: calls.append("b"))
        self.bus.publish("e")
        self.bus.publish("e")
        self.assertEqual(calls, ["a", "b", "b"])

    def test_add_during_dispatch_not_called_this_round(self):
        # 确定规则：本轮新增订阅者不参与本轮，下一轮生效；不漏不重
        calls = []
        state = {}
        def a():
            calls.append("a")
            if state["b"] is None:
                state["b"] = self.bus.subscribe("e", lambda: calls.append("b"))
        state["b"] = None
        self.bus.subscribe("e", a)
        self.bus.subscribe("e", lambda: calls.append("c"))
        self.bus.publish("e")
        self.assertEqual(calls, ["a", "c"])       # b 本轮不被调用
        self.bus.publish("e")
        self.assertEqual(calls, ["a", "c", "a", "c", "b"])  # 下轮按序投递

    def test_remove_during_dispatch_no_duplicate_no_missing(self):
        # A 执行时取消尚未执行的 B：B 本轮不被调用，C 不受影响
        calls = []
        state = {}
        self.bus.subscribe("e", lambda: (calls.append("a"), state["b"].cancel()))
        state["b"] = self.bus.subscribe("e", lambda: calls.append("b"))
        self.bus.subscribe("e", lambda: calls.append("c"))
        self.bus.publish("e")
        self.assertEqual(calls, ["a", "c"])

    def test_recursive_publish_same_event_type(self):
        # 同类型事件递归发布：深度优先，内层完整投递后外层继续
        calls = []
        def handler(n):
            calls.append(n)
            if n < 2:
                self.bus.publish("e", n + 1)
        self.bus.subscribe("e", handler)
        errors = self.bus.publish("e", 0)
        self.assertEqual(errors, [])
        self.assertEqual(calls, [0, 1, 2])


class TestExceptionAggregation(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_exceptions_do_not_interrupt_and_are_aggregated(self):
        calls = []
        def boom1():
            raise ValueError("first")
        def ok():
            calls.append("ok")
        def boom2():
            raise KeyError("second")
        sub1 = self.bus.subscribe("e", boom1)
        self.bus.subscribe("e", ok)
        sub3 = self.bus.subscribe("e", boom2)
        errors = self.bus.publish("e")
        self.assertEqual(calls, ["ok"])  # 其他订阅者不受影响
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(e, DeliveryError) for e in errors))
        self.assertIsInstance(errors[0].exception, ValueError)
        self.assertIsInstance(errors[1].exception, KeyError)
        self.assertIs(errors[0].subscription, sub1)
        self.assertIs(errors[1].subscription, sub3)

    def test_filter_exception_is_aggregated(self):
        calls = []
        self.bus.subscribe("e", lambda: calls.append("x"),
                           filter=lambda: 1 / 0)
        self.bus.subscribe("e", lambda: calls.append("y"))
        errors = self.bus.publish("e")
        self.assertEqual(calls, ["y"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0].exception, ZeroDivisionError)


class TestWeakReference(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_weak_subscription_allows_garbage_collection(self):
        calls = []

        class Listener:
            def on_event(self, v):
                calls.append(v)

        listener = Listener()
        self.bus.subscribe_weak("e", listener.on_event)
        self.bus.publish("e", 1)
        self.assertEqual(calls, [1])
        self.assertEqual(self.bus.subscriber_count("e"), 1)

        ref = weakref.ref(listener)
        del listener
        gc.collect()
        # 对象可回收：这是弱引用订阅的核心目的
        self.assertIsNone(ref())
        # 订阅已自动移除，后续发布不再投递
        self.assertEqual(self.bus.subscriber_count("e"), 0)
        self.bus.publish("e", 2)
        self.assertEqual(calls, [1])

    def test_strong_subscription_prevents_collection(self):
        class Listener:
            def on_event(self):
                pass

        listener = Listener()
        self.bus.subscribe("e", listener.on_event)
        ref = weakref.ref(listener)
        del listener
        gc.collect()
        self.assertIsNotNone(ref())  # 强订阅阻止回收（正是要避免的泄漏）

    def test_weak_subscription_still_respects_cancel(self):
        class Listener:
            def on_event(self):
                pass

        listener = Listener()
        sub = self.bus.subscribe_weak("e", listener.on_event)
        sub.cancel()
        sub.cancel()  # 幂等
        self.assertEqual(self.bus.subscriber_count("e"), 0)


class TestConcurrency(unittest.TestCase):
    def test_concurrent_publish_delivers_all(self):
        bus = EventBus()
        count = 0
        lock = threading.Lock()

        def handler():
            nonlocal count
            with lock:
                count += 1

        bus.subscribe("e", handler)
        n_threads, n_publishes = 8, 250
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            for _ in range(n_publishes):
                bus.publish("e")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(count, n_threads * n_publishes)

    def test_concurrent_once_delivered_exactly_once(self):
        bus = EventBus()
        calls = []
        lock = threading.Lock()

        def handler():
            with lock:
                calls.append(1)

        bus.subscribe_once("e", handler)
        n_threads = 16
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            bus.publish("e")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)  # 并发下至多（且恰好）一次
        self.assertEqual(bus.subscriber_count("e"), 0)

    def test_concurrent_churn_does_not_corrupt_subscription_list(self):
        bus = EventBus()
        stop = threading.Event()
        errors = []

        def publisher():
            try:
                while not stop.is_set():
                    bus.publish("e")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def churn():
            try:
                for _ in range(3000):
                    sub = bus.subscribe("e", lambda: None)
                    sub.cancel()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = ([threading.Thread(target=publisher) for _ in range(3)]
                   + [threading.Thread(target=churn) for _ in range(3)])
        for t in threads:
            t.start()
        for t in threads[3:]:
            t.join()
        stop.set()
        for t in threads[:3]:
            t.join()
        self.assertEqual(errors, [])
        # 结构未被破坏：churn 全部取消后无残留
        self.assertEqual(bus.subscriber_count("e"), 0)
        self.assertEqual(bus.subscriber_count(), 0)

    def test_unsubscribe_racing_publish_never_calls_after_cancel(self):
        # 取消返回后，后续 publish 绝不再调用该订阅者
        bus = EventBus()
        calls = []
        lock = threading.Lock()
        state = {"cancelled": False}

        def handler():
            with lock:
                if state["cancelled"]:
                    calls.append("BAD")

        sub = bus.subscribe("e", handler)
        stop = threading.Event()

        def publisher():
            while not stop.is_set():
                bus.publish("e")

        t = threading.Thread(target=publisher)
        t.start()
        sub.cancel()
        with lock:
            state["cancelled"] = True
        stop.set()
        t.join()
        bus.publish("e")
        self.assertNotIn("BAD", calls)


class TestTopicMatchingRules(unittest.TestCase):
    """通配符匹配规则本身可断言（不依赖总线）。"""

    def test_exact_pattern(self):
        self.assertTrue(topic_matches("user.created", "user.created"))
        self.assertFalse(topic_matches("user.created", "user.deleted"))
        self.assertFalse(topic_matches("user.created", "user.created.x"))
        self.assertFalse(topic_matches("user.created", "user"))

    def test_single_level_wildcard(self):
        self.assertTrue(topic_matches("user.*", "user.created"))
        self.assertFalse(topic_matches("user.*", "user.created.admin"))  # 只匹配一层
        self.assertFalse(topic_matches("user.*", "user"))                # 不能缺层
        self.assertTrue(topic_matches("*.created", "user.created"))      # 前缀层也可通配
        self.assertTrue(topic_matches("user.*.admin", "user.created.admin"))
        self.assertFalse(topic_matches("user.*.admin", "user.created"))

    def test_multi_level_wildcard(self):
        self.assertTrue(topic_matches("user.#", "user.created"))
        self.assertTrue(topic_matches("user.#", "user.created.admin"))
        self.assertTrue(topic_matches("user.#", "user"))      # 匹配零层
        self.assertFalse(topic_matches("user.#", "order.created"))
        self.assertTrue(topic_matches("#", "anything.at.all"))
        self.assertTrue(topic_matches("#", ""))

    def test_non_string_topic_never_matches_pattern(self):
        self.assertFalse(topic_matches("user.*", 42))
        self.assertFalse(topic_matches("#", None))

    def test_invalid_patterns_rejected_at_subscribe(self):
        bus = EventBus()
        for bad in ("user.#.x", "user.cr#", "user.cre*ed", "us*er", "#.#"):
            with self.assertRaises(ValueError, msg=bad):
                bus.subscribe(bad, lambda: None)
        self.assertEqual(bus.subscriber_count(), 0)  # 非法模式不留残留


class TestWildcardSubscription(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_prefix_wildcard_receives_matching_events(self):
        calls = []
        self.bus.subscribe("user.*", lambda t: calls.append(t))
        self.bus.publish("user.created", 1)
        self.bus.publish("user.deleted", 2)
        self.bus.publish("order.created", 3)   # 不匹配
        self.bus.publish("user.created.x", 4)  # 层级不符
        self.assertEqual(calls, [1, 2])

    def test_multi_level_wildcard_receives_subtree(self):
        calls = []
        self.bus.subscribe("user.#", lambda t: calls.append(t))
        self.bus.publish("user.created", 1)
        self.bus.publish("user.created.admin", 2)
        self.bus.publish("user", 3)
        self.bus.publish("order.created", 4)
        self.assertEqual(calls, [1, 2, 3])

    def test_delivery_order_exact_before_wildcard_then_subscription_order(self):
        # 确定规则：先精确后通配，同层按订阅先后；与订阅交错顺序无关
        calls = []
        self.bus.subscribe("user.#", lambda: calls.append("wild-1"))
        self.bus.subscribe("user.created", lambda: calls.append("exact-1"))
        self.bus.subscribe("user.*", lambda: calls.append("wild-2"))
        self.bus.subscribe("user.created", lambda: calls.append("exact-2"))
        self.bus.publish("user.created")
        self.assertEqual(calls, ["exact-1", "exact-2", "wild-1", "wild-2"])

    def test_wildcard_once(self):
        calls = []
        self.bus.subscribe_once("user.*", lambda v: calls.append(v))
        self.bus.publish("user.created", 1)
        self.bus.publish("user.deleted", 2)
        self.assertEqual(calls, [1])
        self.assertEqual(self.bus.subscriber_count(), 0)

    def test_wildcard_once_with_filter_not_consumed_when_filtered_out(self):
        calls = []
        self.bus.subscribe_once("user.#", lambda v: calls.append(v),
                                filter=lambda v: v > 0)
        self.bus.publish("user.created", -1)  # 过滤不通过，不消耗 once
        self.bus.publish("user.updated", 5)
        self.bus.publish("user.deleted", 6)
        self.assertEqual(calls, [5])

    def test_wildcard_filter(self):
        calls = []
        self.bus.subscribe("user.*", lambda v: calls.append(v),
                           filter=lambda v: v % 2 == 0)
        for i in range(4):
            self.bus.publish("user.e", i)
        self.assertEqual(calls, [0, 2])

    def test_wildcard_unsubscribe(self):
        calls = []
        sub = self.bus.subscribe("user.#", lambda: calls.append(1))
        self.bus.publish("user.created")
        sub.cancel()
        self.bus.publish("user.created")
        self.assertEqual(calls, [1])
        self.assertEqual(self.bus.subscriber_count(), 0)

    def test_wildcard_weak_subscription(self):
        calls = []

        class Listener:
            def on_event(self, v):
                calls.append(v)

        listener = Listener()
        self.bus.subscribe_weak("user.*", listener.on_event)
        self.bus.publish("user.created", 1)
        self.assertEqual(calls, [1])
        del listener
        gc.collect()
        self.assertEqual(self.bus.subscriber_count(), 0)
        self.bus.publish("user.created", 2)
        self.assertEqual(calls, [1])

    def test_cancel_wildcard_during_dispatch_takes_effect_immediately(self):
        calls = []
        state = {}
        def exact():
            calls.append("exact")
            state["wild"].cancel()
        self.bus.subscribe("user.created", exact)
        state["wild"] = self.bus.subscribe("user.*", lambda: calls.append("wild"))
        self.bus.subscribe("user.#", lambda: calls.append("wild2"))
        self.bus.publish("user.created")
        self.assertEqual(calls, ["exact", "wild2"])  # 取消立即生效

    def test_non_string_event_type_unaffected(self):
        # 非字符串事件类型维持精确匹配语义，不参与通配
        calls = []
        self.bus.subscribe("#", lambda: calls.append("wild"))
        self.bus.subscribe(42, lambda: calls.append("exact"))
        self.bus.publish(42)
        self.assertEqual(calls, ["exact"])

    def test_subscriber_count_includes_matching_wildcards(self):
        self.bus.subscribe("user.created", lambda: None)
        self.bus.subscribe("user.*", lambda: None)
        self.bus.subscribe("user.#", lambda: None)
        self.bus.subscribe("order.#", lambda: None)
        self.assertEqual(self.bus.subscriber_count("user.created"), 3)
        self.assertEqual(self.bus.subscriber_count("user.created.admin"), 1)
        self.assertEqual(self.bus.subscriber_count(), 4)


class TestSubscriptionQuery(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_query_all_subscriptions(self):
        s1 = self.bus.subscribe("user.created", lambda: None)
        s2 = self.bus.subscribe("user.*", lambda: None)
        s3 = self.bus.subscribe("order.#", lambda: None)
        subs = self.bus.subscriptions()
        self.assertEqual(subs, [s1, s2, s3])  # 先精确后通配，各自按订阅顺序
        self.assertEqual([s.event_type for s in subs],
                         ["user.created", "user.*", "order.#"])
        self.assertEqual([s.wildcard for s in subs], [False, True, True])

    def test_query_by_topic_reflects_delivery_order(self):
        multi = self.bus.subscribe("user.#", lambda: None)
        exact = self.bus.subscribe("user.created", lambda: None)
        wild = self.bus.subscribe("user.*", lambda: None)
        subs = self.bus.subscriptions("user.created")
        self.assertEqual(subs, [exact, multi, wild])  # 通配层按订阅先后
        self.assertEqual([s.event_type for s in subs],
                         ["user.created", "user.#", "user.*"])

    def test_query_excludes_cancelled(self):
        s1 = self.bus.subscribe("user.created", lambda: None)
        s2 = self.bus.subscribe("user.#", lambda: None)
        s1.cancel()
        self.assertEqual(self.bus.subscriptions(), [s2])
        self.assertEqual(self.bus.subscriptions("user.created"), [s2])
        s2.cancel()
        self.assertEqual(self.bus.subscriptions(), [])

    def test_clear_removes_wildcard_subscriptions(self):
        self.bus.subscribe("user.*", lambda: None)
        self.bus.subscribe("user.created", lambda: None)
        self.bus.clear()
        self.assertEqual(self.bus.subscriptions(), [])
        self.assertEqual(self.bus.subscriber_count(), 0)


if __name__ == "__main__":
    unittest.main()
