"""EDF 调度器自测（仅标准库 unittest，时间由 FakeClock 注入）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import FakeClock, Scheduler, Status, tolerable_wait


def drain(scheduler):
    """把队列中所有可执行任务按出队顺序取出。"""
    out = []
    while True:
        result = scheduler.dequeue()
        if result is None or result.task is None:
            break
        out.append(result.task)
    return out


class SchedulingOrderTests(unittest.TestCase):
    def test_earliest_deadline_first(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("late", 2, deadline=10)
        sched.submit("early", 2, deadline=5)
        sched.submit("mid", 2, deadline=7)

        self.assertEqual([t.id for t in drain(sched)], ["early", "mid", "late"])

    def test_same_deadline_keeps_submission_order(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("a", 1, deadline=5)
        sched.submit("b", 1, deadline=5)
        sched.submit("c", 1, deadline=5)

        self.assertEqual([t.id for t in drain(sched)], ["a", "b", "c"])

    def test_no_deadline_tasks_last_and_fifo(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("none1", 1)
        sched.submit("has", 1, deadline=100)
        sched.submit("none2", 1)
        sched.submit("none3", 1)
        sched.submit("early", 1, deadline=50)

        self.assertEqual(
            [t.id for t in drain(sched)],
            ["early", "has", "none1", "none2", "none3"],
        )

    def test_mixed_ordering_with_new_submissions_over_time(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("a", 1, deadline=10)
        clock.advance(1)
        sched.submit("b", 1, deadline=5)  # 更紧急
        sched.submit("c", 1)
        result = sched.dequeue()
        self.assertEqual(result.task.id, "b")


class OverdueAndAlertTests(unittest.TestCase):
    def test_overdue_task_evicted_on_dequeue_and_alerted_once(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("tight", duration=5, deadline=3)   # 提交时即超期
        sched.submit("ok", duration=2, deadline=10)

        result = sched.dequeue()
        # 超期任务不得继续排队等待：ok 被返回，tight 被驱逐
        self.assertEqual(result.task.id, "ok")
        self.assertEqual([t.id for t in result.overdue], ["tight"])
        self.assertEqual(sched.status_of("tight"), Status.OVERDUE)
        self.assertEqual(sched.pending_count(), 0)

        # 关键断言：告警恰好一次
        self.assertEqual(sched.alert_count(), 1)
        self.assertEqual([a.task_id for a in sched.alerts], ["tight"])
        alert = sched.alerts[0]
        self.assertEqual(alert.remaining, 3)
        self.assertEqual(alert.tolerable_wait, -2)

        # 重复出队不得再次告警
        self.assertIsNone(sched.dequeue())
        self.assertEqual(sched.alert_count(), 1)

        # 时间继续推进也不得重复告警
        clock.advance(100)
        self.assertEqual(sched.tick(), ())
        self.assertIsNone(sched.dequeue())
        self.assertEqual(sched.alert_count(), 1)

    def test_alert_fires_only_when_threshold_crossed(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("t", duration=4, deadline=10)

        # 仍可容忍 1 个单位等待，不告警
        clock.jump_to(5)
        self.assertEqual(sched.tick(), ())
        self.assertEqual(sched.alert_count(), 0)

        # 恰好可完成（可容忍等待 = 0），仍不告警，且正常出队
        clock.jump_to(6)
        result = sched.dequeue()
        self.assertEqual(result.task.id, "t")
        self.assertEqual(result.tolerable_wait, 0.0)
        self.assertEqual(sched.alert_count(), 0)

    def test_clock_jump_spans_multiple_tasks(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        for i in range(5):
            sched.submit(f"d{i}", duration=2, deadline=10 + i)
        sched.submit("survivor", duration=3, deadline=100)
        sched.submit("nodead", duration=1)

        # 一次跳跃跨越 d0..d4（它们在 t=50 时均无法完成）
        clock.jump_to(50)
        overdue = sched.tick()
        self.assertEqual([t.id for t in overdue], [f"d{i}" for i in range(5)])
        self.assertEqual(sched.alert_count(), 5)

        # 无截止时间任务不被超期驱逐；survivor 仍可完成，优先出队
        result = sched.dequeue()
        self.assertEqual(result.task.id, "survivor")
        self.assertEqual(result.overdue, ())  # 上一次 tick 已驱逐完
        result = sched.dequeue()
        self.assertEqual(result.task.id, "nodead")
        self.assertIsNone(sched.dequeue())

        # 时间再跳，告警数不变
        clock.jump_to(500)
        sched.tick()
        self.assertEqual(sched.alert_count(), 5)

    def test_overdue_during_drain_each_alerted_once(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("a", duration=10, deadline=5)
        sched.submit("b", duration=10, deadline=6)
        sched.submit("c", duration=10, deadline=7)

        # 全部超期；每次 dequeue 驱逐队首超期任务
        r1 = sched.dequeue()
        self.assertIsNone(r1.task)
        self.assertEqual([t.id for t in r1.overdue], ["a", "b", "c"])
        self.assertEqual(sched.alert_count(), 3)
        self.assertIsNone(sched.dequeue())
        self.assertEqual(sched.alert_count(), 3)

    def test_no_deadline_never_overdue(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("forever", duration=1)
        clock.jump_to(10_000)
        self.assertEqual(sched.tick(), ())
        result = sched.dequeue()
        self.assertEqual(result.task.id, "forever")
        self.assertEqual(result.tolerable_wait, float("inf"))
        self.assertEqual(sched.alert_count(), 0)


class CancelTests(unittest.TestCase):
    def test_canceled_pending_task_excluded_from_ordering(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("urgent", duration=1, deadline=1)
        sched.submit("next", duration=1, deadline=2)

        self.assertEqual(sched.cancel("urgent"), Status.CANCELED)

        result = sched.dequeue()
        self.assertEqual(result.task.id, "next")
        self.assertEqual(sched.pending_count(), 0)
        self.assertEqual(sched.alert_count(), 0)

    def test_canceled_task_never_alerts_even_after_jump(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("x", duration=5, deadline=3)
        sched.submit("y", duration=5, deadline=4)
        sched.cancel("x")
        clock.jump_to(100)

        overdue = sched.tick()
        self.assertEqual([t.id for t in overdue], ["y"])
        self.assertEqual(sched.alert_count(), 1)  # 已取消的 x 不告警
        self.assertEqual(sched.status_of("x"), Status.CANCELED)

    def test_cancel_overdue_task_has_deterministic_result(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("late", duration=10, deadline=1)
        clock.jump_to(50)
        sched.tick()
        self.assertEqual(sched.status_of("late"), Status.OVERDUE)
        alerts_before = sched.alert_count()

        # 已超期任务取消：终态确定为 OVERDUE，告警既不撤销也不补发
        self.assertEqual(sched.cancel("late"), Status.OVERDUE)
        self.assertEqual(sched.status_of("late"), Status.OVERDUE)
        self.assertEqual(sched.alert_count(), alerts_before)
        self.assertIsNone(sched.dequeue())

    def test_complete_lifecycle(self):
        clock = FakeClock(0)
        sched = Scheduler(clock)
        sched.submit("z", duration=1, deadline=10)
        result = sched.dequeue()
        sched.complete("z")
        self.assertEqual(sched.status_of("z"), Status.COMPLETED)
        with self.assertRaises(ValueError):
            sched.complete("z")


class TolerableWaitHelperTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(tolerable_wait(0, 10, 4), 6)
        self.assertEqual(tolerable_wait(6, 10, 4), 0)
        self.assertEqual(tolerable_wait(8, 10, 4), -2)
        self.assertEqual(tolerable_wait(0, None, 4), float("inf"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
