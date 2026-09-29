"""taskrunner 回归测试。

覆盖：
- 五种状态：PENDING / RUNNING / SUCCEEDED / FAILED / CANCELLED
- 三种竞争时序：开始前取消、执行中取消、取消与完成同时竞争
- 重复取消、取消已完成任务、任务内自取消
- 成功/失败/取消三条路径的资源释放
- 适配层与结果对象之间的异常等价性，以及与重构前 legacy 实现逐例比对
"""

import threading
import time
import unittest

from legacy import LegacyTaskRunner
from taskrunner import CancelledError, TaskResult, TaskRunner, TaskState


class CustomError(Exception):
    pass


def run_legacy(fn):
    """运行重构前实现，捕获回调结果。"""
    outcome = {}

    def on_success(value):
        outcome["value"] = value

    def on_error(exc):
        outcome["error"] = exc

    runner = LegacyTaskRunner()
    thread = runner.submit(fn, on_success, on_error)
    thread.join(timeout=5)
    return outcome, runner


class TestFiveStates(unittest.TestCase):
    def test_pending(self):
        result = TaskResult()
        self.assertIs(result.state(), TaskState.PENDING)
        self.assertFalse(result.done())

        gate = threading.Event()
        runner = TaskRunner(start_gate=gate)
        submitted = runner.submit(lambda ctx: 1)
        self.assertIs(submitted.state(), TaskState.PENDING)
        gate.set()
        self.assertTrue(submitted.wait(5))

    def test_running(self):
        entered = threading.Event()
        release = threading.Event()

        def fn(ctx):
            entered.set()
            release.wait(5)
            return 1

        runner = TaskRunner()
        result = runner.submit(fn)
        self.assertTrue(entered.wait(5))
        self.assertIs(result.state(), TaskState.RUNNING)
        release.set()
        self.assertTrue(result.wait(5))

    def test_succeeded(self):
        runner = TaskRunner()
        result = runner.submit(lambda ctx: 42)
        self.assertTrue(result.wait(5))
        self.assertIs(result.state(), TaskState.SUCCEEDED)
        self.assertEqual(result.result(), 42)
        self.assertIsNone(result.error())

    def test_failed(self):
        def fn(ctx):
            raise ValueError("bad input")

        runner = TaskRunner()
        result = runner.submit(fn)
        self.assertTrue(result.wait(5))
        self.assertIs(result.state(), TaskState.FAILED)
        err = result.error()
        self.assertIs(type(err), ValueError)
        self.assertEqual(str(err), "bad input")
        with self.assertRaises(ValueError) as cm:
            result.result()
        self.assertIs(cm.exception, err)  # 同一个异常对象，信息不丢失

    def test_cancelled(self):
        gate = threading.Event()
        runner = TaskRunner(start_gate=gate)
        result = runner.submit(lambda ctx: 1)
        self.assertTrue(result.cancel())
        gate.set()
        self.assertTrue(result.wait(5))
        self.assertIs(result.state(), TaskState.CANCELLED)
        with self.assertRaises(CancelledError):
            result.result()


class TestRaceTimings(unittest.TestCase):
    """三种竞争时序。"""

    def test_cancel_before_start(self):
        """时序 1：取消发生在任务开始前（PENDING -> CANCELLED）。"""
        gate = threading.Event()
        runner = TaskRunner(start_gate=gate)
        result = runner.submit(lambda ctx: 1)
        self.assertIs(result.state(), TaskState.PENDING)
        self.assertTrue(result.cancel())
        gate.set()
        self.assertTrue(result.wait(5))
        self.assertIs(result.state(), TaskState.CANCELLED)
        self.assertEqual(runner.resources, [])  # 未获取任何资源

    def test_join_unblocks_when_cancelled_before_start(self):
        """回归：开始前取消的任务，join() 不得永久等待清理信号。"""
        gate = threading.Event()
        runner = TaskRunner(start_gate=gate)
        result = runner.submit(lambda ctx: 1)
        self.assertTrue(result.cancel())
        gate.set()
        # worker 提前返回的分支也必须置位清理完成信号
        self.assertTrue(result.join(5))
        self.assertIs(result.state(), TaskState.CANCELLED)

    def test_cancel_during_run(self):
        """时序 2：取消发生在执行中（RUNNING -> CANCELLED），worker 不得覆盖。"""
        entered = threading.Event()
        release = threading.Event()

        def fn(ctx):
            entered.set()
            release.wait(5)
            return "should-be-discarded"

        runner = TaskRunner()
        result = runner.submit(fn)
        self.assertTrue(entered.wait(5))
        self.assertTrue(result.cancel())
        release.set()
        self.assertTrue(result.join(5))
        self.assertIs(result.state(), TaskState.CANCELLED)
        with self.assertRaises(CancelledError):
            result.result()
        self.assertEqual(len(runner.resources), 1)
        self.assertTrue(all(r.closed for r in runner.resources))

    def test_cancel_races_completion(self):
        """时序 3：取消与完成同时竞争 —— 先到先得，结果唯一确定。"""
        for _ in range(300):
            runner = TaskRunner()
            about_to_finish = threading.Event()

            def fn(ctx):
                about_to_finish.set()
                return "done"

            result = runner.submit(fn)
            self.assertTrue(about_to_finish.wait(5))
            won = result.cancel()  # 与 worker 的 settle 竞争
            self.assertTrue(result.join(5))

            if won:
                # 取消先到：状态必须是 CANCELLED，结果不得被写入
                self.assertIs(result.state(), TaskState.CANCELLED)
                with self.assertRaises(CancelledError):
                    result.result()
            else:
                # 完成先到：取消必须是幂等空操作，结果完整
                self.assertIs(result.state(), TaskState.SUCCEEDED)
                self.assertEqual(result.result(), "done")
            # 两种结局下资源都必须释放
            self.assertTrue(all(r.closed for r in runner.resources))


class TestCancellationEdgeCases(unittest.TestCase):
    def test_repeated_cancel(self):
        gate = threading.Event()
        runner = TaskRunner(start_gate=gate)
        result = runner.submit(lambda ctx: 1)
        self.assertTrue(result.cancel())
        self.assertFalse(result.cancel())  # 重复取消：幂等
        self.assertFalse(result.cancel())
        self.assertIs(result.state(), TaskState.CANCELLED)
        gate.set()
        self.assertTrue(result.wait(5))
        self.assertIs(result.state(), TaskState.CANCELLED)

    def test_cancel_after_completion(self):
        runner = TaskRunner()
        result = runner.submit(lambda ctx: 7)
        self.assertTrue(result.wait(5))
        self.assertFalse(result.cancel())  # 已完成任务的取消是空操作
        self.assertIs(result.state(), TaskState.SUCCEEDED)
        self.assertEqual(result.result(), 7)

    def test_self_cancel(self):
        """任务内自取消：返回值不得被写入，资源照常释放。"""
        def fn(ctx):
            ctx.cancel()
            return "should-be-discarded"

        runner = TaskRunner()
        result = runner.submit(fn)
        self.assertTrue(result.join(5))
        self.assertIs(result.state(), TaskState.CANCELLED)
        with self.assertRaises(CancelledError):
            result.result()
        self.assertEqual(len(runner.resources), 1)
        self.assertTrue(runner.resources[0].closed)


class TestResourceCleanup(unittest.TestCase):
    def assert_all_released(self, runner):
        self.assertTrue(runner.resources, "expected at least one resource")
        self.assertTrue(all(r.closed for r in runner.resources))

    def test_released_on_success(self):
        runner = TaskRunner()
        self.assertTrue(runner.submit(lambda ctx: 1).join(5))
        self.assert_all_released(runner)

    def test_released_on_failure(self):
        def fn(ctx):
            raise RuntimeError("boom")

        runner = TaskRunner()
        self.assertTrue(runner.submit(fn).join(5))
        self.assert_all_released(runner)

    def test_released_on_cancel(self):
        entered = threading.Event()
        release = threading.Event()

        def fn(ctx):
            entered.set()
            release.wait(5)

        runner = TaskRunner()
        result = runner.submit(fn)
        self.assertTrue(entered.wait(5))
        result.cancel()
        release.set()
        self.assertTrue(result.join(5))
        self.assert_all_released(runner)


class TestCallbackAdapter(unittest.TestCase):
    def collect(self):
        calls = []
        event = threading.Event()

        def make(tag):
            def cb(arg=None):
                calls.append((tag, arg))
                event.set()
            return cb

        return calls, event, make("success"), make("error"), make("cancel")

    def test_adapter_success(self):
        calls, event, ok, err, cancel = self.collect()
        runner = TaskRunner()
        runner.submit_callback(lambda ctx: 42, ok, err, cancel)
        self.assertTrue(event.wait(5))
        time.sleep(0.05)
        self.assertEqual(calls, [("success", 42)])

    def test_adapter_failure(self):
        calls, event, ok, err, cancel = self.collect()
        exc = ValueError("bad")

        def fn(ctx):
            raise exc

        runner = TaskRunner()
        runner.submit_callback(fn, ok, err, cancel)
        self.assertTrue(event.wait(5))
        time.sleep(0.05)
        self.assertEqual(len(calls), 1)
        tag, got = calls[0]
        self.assertEqual(tag, "error")
        self.assertIs(got, exc)  # 同一个异常对象

    def test_adapter_no_callback_after_cancel(self):
        """取消生效后不得再触发成功/失败回调，恰好触发一次 on_cancel。"""
        calls, event, ok, err, cancel = self.collect()
        entered = threading.Event()
        release = threading.Event()

        def fn(ctx):
            entered.set()
            release.wait(5)
            return 1

        runner = TaskRunner()
        result = runner.submit_callback(fn, ok, err, cancel)
        self.assertTrue(entered.wait(5))
        self.assertTrue(result.cancel())
        release.set()
        self.assertTrue(event.wait(5))
        time.sleep(0.05)
        self.assertEqual(calls, [("cancel", None)])


class TestEquivalenceWithLegacy(unittest.TestCase):
    """异常与结果在 legacy、结果对象、适配层三者之间逐例比对。"""

    CASES = {
        "success_int": lambda: 42,
        "success_none": lambda: None,
        "success_dict": lambda: {"k": [1, 2]},
        "value_error": lambda: (_ for _ in ()).throw(ValueError("bad input")),
        "key_error": lambda: (_ for _ in ()).throw(KeyError("missing")),
        "unicode_error": lambda: (_ for _ in ()).throw(RuntimeError("错误：磁盘满")),
        "custom_error": lambda: (_ for _ in ()).throw(CustomError("code=7")),
    }

    def run_new_result_object(self, fn):
        runner = TaskRunner()
        result = runner.submit(lambda ctx: fn())
        self.assertTrue(result.wait(5))
        if result.state() is TaskState.SUCCEEDED:
            return {"value": result.result()}
        return {"error": result.error()}

    def run_new_adapter(self, fn):
        outcome = {}
        event = threading.Event()

        def on_success(value):
            outcome["value"] = value
            event.set()

        def on_error(exc):
            outcome["error"] = exc
            event.set()

        runner = TaskRunner()
        runner.submit_callback(lambda ctx: fn(), on_success, on_error)
        self.assertTrue(event.wait(5))
        return outcome

    def test_equivalence(self):
        for name, fn in self.CASES.items():
            with self.subTest(case=name):
                legacy_outcome, _ = run_legacy(fn)
                via_result = self.run_new_result_object(fn)
                via_adapter = self.run_new_adapter(fn)

                if "value" in legacy_outcome:
                    self.assertEqual(via_result["value"], legacy_outcome["value"])
                    self.assertEqual(via_adapter["value"], legacy_outcome["value"])
                else:
                    legacy_err = legacy_outcome["error"]
                    for got in (via_result["error"], via_adapter["error"]):
                        # 错误类型与消息必须与重构前一致
                        self.assertIs(type(got), type(legacy_err))
                        self.assertEqual(str(got), str(legacy_err))
                        self.assertEqual(got.args, legacy_err.args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
