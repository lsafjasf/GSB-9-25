"""回归测试：五种状态、三种竞争时序、资源释放、异常等价性。

运行：python3 -m unittest test_regression -v
"""
import threading
import time
import unittest

import legacy_task
from resources import ResourceTracker
from task_result import (
    CancelledError,
    TaskState,
    submit,
    submit_with_callback,
)


def make_task(value=42, delay=0.0):
    def fn(resource, token):
        if delay:
            time.sleep(delay)
        resource.write(b"data")
        return value
    return fn


class StateTests(unittest.TestCase):
    """覆盖五种状态：未开始、执行中、成功、失败、已取消。"""

    def setUp(self):
        self.tracker = ResourceTracker()

    def test_pending(self):
        gate = threading.Event()
        handle = submit(make_task(), tracker=self.tracker, start_gate=gate)
        self.assertEqual(handle.result_object.state, TaskState.PENDING)
        gate.set()
        self.assertEqual(handle.result_object.result(2), 42)

    def test_running(self):
        entered = threading.Event()
        release = threading.Event()

        def fn(resource, token):
            entered.set()
            release.wait(2)
            return 1

        handle = submit(fn, tracker=self.tracker)
        entered.wait(2)
        self.assertEqual(handle.result_object.state, TaskState.RUNNING)
        release.set()
        self.assertEqual(handle.result_object.result(2), 1)
        self.assertEqual(handle.result_object.state, TaskState.SUCCESS)

    def test_success(self):
        handle = submit(make_task(value="ok"), tracker=self.tracker)
        self.assertEqual(handle.result_object.result(2), "ok")
        self.assertEqual(handle.result_object.state, TaskState.SUCCESS)
        self.assertIsNone(handle.result_object.error)
        self.assertTrue(handle.result_object.done())

    def test_failed(self):
        def fn(resource, token):
            raise ValueError("boom")

        handle = submit(fn, tracker=self.tracker)
        with self.assertRaises(ValueError) as ctx:
            handle.result_object.result(2)
        self.assertEqual(str(ctx.exception), "boom")
        self.assertEqual(handle.result_object.state, TaskState.FAILED)
        self.assertIs(handle.result_object.error, ctx.exception)

    def test_cancelled(self):
        gate = threading.Event()
        handle = submit(make_task(), tracker=self.tracker, start_gate=gate)
        self.assertTrue(handle.cancel())
        gate.set()
        self.assertTrue(handle.result_object.wait(2))
        self.assertEqual(handle.result_object.state, TaskState.CANCELLED)
        with self.assertRaises(CancelledError):
            handle.result_object.result(2)


class RaceTests(unittest.TestCase):
    """三种竞争时序：开始前取消 / 执行中与完成竞争 / 完成后取消。"""

    def setUp(self):
        self.tracker = ResourceTracker()

    def test_cancel_before_start(self):
        callbacks = []
        gate = threading.Event()
        handle = submit(make_task(), callback=lambda e, r: callbacks.append((e, r)),
                        tracker=self.tracker, start_gate=gate)
        self.assertTrue(handle.cancel())       # 取消先到
        gate.set()
        self.assertTrue(handle.result_object.wait(2))
        self.assertEqual(handle.result_object.state, TaskState.CANCELLED)
        self.assertEqual(callbacks, [])        # 取消后不得触发回调
        self.assertTrue(handle.join(2))
        self.assertEqual(self.tracker.live_count, 0)

    def test_cancel_races_completion(self):
        """执行中取消与完成竞争：先到先得，两种结局都合法但必须自洽。"""
        outcomes = set()
        for _ in range(300):
            tracker = ResourceTracker()
            callbacks = []
            proceed = threading.Event()

            def fn(resource, token):
                proceed.wait(2)          # 等取消线程就位后同时放行
                resource.write(b"x")
                return 7

            handle = submit(fn, callback=lambda e, r: callbacks.append((e, r)),
                            tracker=tracker)
            canceller = threading.Thread(target=handle.cancel)
            canceller.start()
            proceed.set()                # 完成与取消同时竞争
            canceller.join()
            self.assertTrue(handle.result_object.wait(2))
            state = handle.result_object.state
            outcomes.add(state)
            if state is TaskState.SUCCESS:
                # 完成先到：结果已写入，回调恰好触发一次，取消不得翻盘
                self.assertEqual(handle.result_object.result(), 7)
                self.assertEqual(callbacks, [(None, 7)])
            else:
                # 取消先到：不得写结果、不得触发回调
                self.assertEqual(state, TaskState.CANCELLED)
                with self.assertRaises(CancelledError):
                    handle.result_object.result()
                self.assertEqual(callbacks, [])
            self.assertFalse(handle.cancel())   # 终态后取消无效
            self.assertEqual(handle.result_object.state, state)  # 状态稳定
            self.assertTrue(handle.join(2))
            self.assertEqual(tracker.live_count, 0)

    def test_cancel_after_complete(self):
        callbacks = []
        handle = submit(make_task(value=9), callback=lambda e, r: callbacks.append((e, r)),
                        tracker=self.tracker)
        self.assertEqual(handle.result_object.result(2), 9)
        self.assertFalse(handle.cancel())      # 已完成，取消失败
        self.assertEqual(handle.result_object.state, TaskState.SUCCESS)
        self.assertEqual(handle.result_object.result(), 9)  # 结果不被覆盖
        self.assertEqual(callbacks, [(None, 9)])


class CancelSemanticsTests(unittest.TestCase):
    """重复取消、取消已完成任务、任务内自取消。"""

    def setUp(self):
        self.tracker = ResourceTracker()

    def test_repeated_cancel_is_idempotent(self):
        gate = threading.Event()
        handle = submit(make_task(), tracker=self.tracker, start_gate=gate)
        self.assertTrue(handle.cancel())       # 第一次生效
        self.assertFalse(handle.cancel())      # 之后幂等返回 False
        self.assertFalse(handle.cancel())
        gate.set()
        self.assertTrue(handle.result_object.wait(2))
        self.assertEqual(handle.result_object.state, TaskState.CANCELLED)
        self.assertTrue(handle.join(2))
        self.assertEqual(self.tracker.live_count, 0)

    def test_cancel_completed_task(self):
        handle = submit(make_task(value=5), tracker=self.tracker)
        self.assertEqual(handle.result_object.result(2), 5)
        self.assertFalse(handle.cancel())
        self.assertEqual(handle.result_object.state, TaskState.SUCCESS)

    def test_self_cancel_inside_task(self):
        callbacks = []

        def fn(resource, token):
            token.cancel()                     # 任务内自取消
            resource.write(b"partial")
            return 1                           # 即使正常返回也不得写结果

        handle = submit(fn, callback=lambda e, r: callbacks.append((e, r)),
                        tracker=self.tracker)
        self.assertTrue(handle.result_object.wait(2))
        self.assertEqual(handle.result_object.state, TaskState.CANCELLED)
        with self.assertRaises(CancelledError):
            handle.result_object.result()
        self.assertEqual(callbacks, [])
        self.assertTrue(handle.join(2))
        self.assertEqual(self.tracker.live_count, 0)

    def test_self_cancel_via_throw(self):
        def fn(resource, token):
            token.cancel()
            token.throw_if_cancelled()         # 协作式抛 CancelledError

        handle = submit(fn, tracker=self.tracker)
        self.assertTrue(handle.result_object.wait(2))
        self.assertEqual(handle.result_object.state, TaskState.CANCELLED)
        self.assertIsInstance(handle.result_object.error, CancelledError)
        self.assertTrue(handle.join(2))
        self.assertEqual(self.tracker.live_count, 0)


class ResourceLeakTests(unittest.TestCase):
    """无论成功、失败、取消，句柄与缓冲都必须释放。"""

    def test_no_leak_on_success(self):
        tracker = ResourceTracker()
        handle = submit(make_task(), tracker=tracker)
        handle.result_object.result(2)
        self.assertTrue(handle.join(2))
        self.assertEqual(tracker.live_count, 0)

    def test_no_leak_on_failure(self):
        tracker = ResourceTracker()

        def fn(resource, token):
            resource.write(b"x")
            raise RuntimeError("fail")

        handle = submit(fn, tracker=tracker)
        with self.assertRaises(RuntimeError):
            handle.result_object.result(2)
        self.assertTrue(handle.join(2))
        self.assertEqual(tracker.live_count, 0)

    def test_no_leak_on_cancel(self):
        tracker = ResourceTracker()
        gate = threading.Event()
        handle = submit(make_task(), tracker=tracker, start_gate=gate)
        handle.cancel()
        gate.set()
        self.assertTrue(handle.result_object.wait(2))
        self.assertTrue(handle.join(2))
        self.assertEqual(tracker.live_count, 0)

    def test_no_leak_on_self_cancel(self):
        tracker = ResourceTracker()

        def fn(resource, token):
            token.cancel()
            return 1

        handle = submit(fn, tracker=tracker)
        self.assertTrue(handle.result_object.wait(2))
        self.assertTrue(handle.join(2))
        self.assertEqual(tracker.live_count, 0)


class EquivalenceTests(unittest.TestCase):
    """异常与结果在旧实现、适配层、结果对象之间逐例比对。"""

    CASES = [
        ("value_error", lambda r: (_ for _ in ()).throw(ValueError("boom"))),
        ("key_error", lambda r: (_ for _ in ()).throw(KeyError("missing"))),
        ("runtime_multi_arg", lambda r: (_ for _ in ()).throw(RuntimeError("a", 1))),
        ("custom", lambda r: (_ for _ in ()).throw(ArithmeticError("div"))),
        ("success", lambda r: "done"),
    ]

    def _run_legacy(self, fn):
        got = []
        done = threading.Event()
        legacy_task.submit_task(fn, lambda e, r: (got.append((e, r)), done.set()))
        self.assertTrue(done.wait(2))
        return got[0]

    def _run_adapter(self, fn):
        got = []
        done = threading.Event()
        submit_with_callback(fn, lambda e, r: (got.append((e, r)), done.set()))
        self.assertTrue(done.wait(2))
        return got[0]

    def _run_result_object(self, fn):
        handle = submit(lambda resource, token: fn(resource))
        try:
            return None, handle.result_object.result(2)
        except Exception as exc:
            return exc, None

    def test_error_and_result_equivalence(self):
        for name, fn in self.CASES:
            with self.subTest(case=name):
                legacy_err, legacy_res = self._run_legacy(fn)
                adapter_err, adapter_res = self._run_adapter(fn)
                result_err, result_res = self._run_result_object(fn)

                # 适配层 vs 旧实现：错误类型与消息一致，结果一致
                self.assertEqual(type(adapter_err), type(legacy_err))
                self.assertEqual(str(adapter_err), str(legacy_err))
                self.assertEqual(adapter_res, legacy_res)

                # 结果对象 vs 旧实现：错误类型与消息一致，结果一致
                self.assertEqual(type(result_err), type(legacy_err))
                self.assertEqual(str(result_err), str(legacy_err))
                self.assertEqual(result_res, legacy_res)

    def test_callback_fired_exactly_once(self):
        for name, fn in self.CASES:
            with self.subTest(case=name):
                calls = []
                done = threading.Event()
                submit_with_callback(fn, lambda e, r: (calls.append(1), done.set()))
                self.assertTrue(done.wait(2))
                time.sleep(0.05)
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
